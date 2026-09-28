"""Run one isolated boot-contract QEMU validation mode."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping, Sequence

from harness.contract import ContractEvidenceError, MODES, validate_contract_log
from harness.qemu import (
    HarnessError,
    ProgressWatchdog,
    QmpClient,
    _host_event,
    _stop_qemu,
    _stream_serial,
    _wait_for_qemu,
    _write_event_log,
)
from harness.transport import SHA_PATTERN
from harness.transport_pins import SourcePin, SourcePinError, verify_source_pins


def _numeric(environ: Mapping[str, str], name: str, default: int) -> int:
    value = environ.get(name, str(default))
    if not value.isdigit():
        raise HarnessError(f"{name} must be numeric")
    return int(value)


@dataclass(frozen=True)
class ContractConfig:
    root: Path
    build_dir: Path
    qemu: str
    cpus: int
    memory_mb: int
    timeout_seconds: int
    idle_timeout_seconds: int
    kernel_sha: str
    fixture_sha: str
    linux_dir: Path
    fixture_dir: Path
    mode: str

    @classmethod
    def from_environment(
        cls, root: Path, mode: str, environ: Mapping[str, str] = os.environ
    ) -> "ContractConfig":
        config = cls(
            root=root,
            build_dir=Path(environ.get("BUILD_DIR", root / "build")),
            qemu=environ.get("QEMU", "qemu-system-x86_64"),
            cpus=_numeric(environ, "QEMU_CPUS", 12),
            memory_mb=_numeric(environ, "QEMU_MEMORY_MB", 8192),
            timeout_seconds=_numeric(environ, "QEMU_TIMEOUT", 900),
            idle_timeout_seconds=_numeric(environ, "QEMU_IDLE_TIMEOUT", 120),
            kernel_sha=environ.get("CONTRACT_KERNEL_SHA", ""),
            fixture_sha=environ.get("CONTRACT_FIXTURE_SHA", ""),
            linux_dir=Path(environ.get("CONTRACT_LINUX_DIR", "")),
            fixture_dir=Path(environ.get("CONTRACT_FIXTURE_DIR", "")),
            mode=mode,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.mode not in MODES:
            raise HarnessError(f"unknown contract mode: {self.mode}")
        if self.cpus < 3:
            raise HarnessError("contract QEMU requires at least 3 CPUs")
        if self.memory_mb < 2048:
            raise HarnessError("contract QEMU requires at least 2048 MiB")
        if self.timeout_seconds < 30 or self.idle_timeout_seconds < 1:
            raise HarnessError("invalid contract timeout")
        for name, value in (
            ("CONTRACT_KERNEL_SHA", self.kernel_sha),
            ("CONTRACT_FIXTURE_SHA", self.fixture_sha),
        ):
            if not SHA_PATTERN.fullmatch(value):
                raise HarnessError(f"{name} must be a full 40-character SHA")

    def verify_sources(self) -> None:
        try:
            verify_source_pins(
                (
                    SourcePin("fixture", self.fixture_dir, self.fixture_sha),
                    SourcePin("linux", self.linux_dir, self.kernel_sha),
                )
            )
        except SourcePinError as error:
            raise HarnessError(str(error)) from error

    @property
    def kernel(self) -> Path:
        return self.build_dir / "kernel/arch/x86/boot/bzImage"

    @property
    def initrd(self) -> Path:
        return self.build_dir / "host-initrd.cpio.gz"

    @property
    def log(self) -> Path:
        return self.build_dir / f"contract-{self.mode}-{self.kernel_sha}.log"

    @property
    def event_log(self) -> Path:
        return self.build_dir / f"contract-{self.mode}-{self.kernel_sha}.jsonl"

    @property
    def qmp_socket(self) -> Path:
        return self.build_dir / f"contract-{self.mode}-qmp.sock"

    def qemu_args(self) -> list[str]:
        return [
            "-machine",
            "q35,accel=tcg",
            "-cpu",
            "max",
            "-smp",
            str(self.cpus),
            "-m",
            str(self.memory_mb),
            "-kernel",
            str(self.kernel),
            "-initrd",
            str(self.initrd),
            "-append",
            "console=ttyS0,115200 rdinit=/init panic=-1 "
            f"mk_contract_test={self.mode} "
            f"mk_contract_kernel_sha={self.kernel_sha} "
            f"mk_contract_fixture_sha={self.fixture_sha}",
            "-nographic",
            "-monitor",
            "none",
            "-qmp",
            f"unix:{self.qmp_socket},server=on,wait=off",
            "-no-reboot",
        ]


async def _run_async(config: ContractConfig) -> int:
    config.build_dir.mkdir(parents=True, exist_ok=True)
    config.qmp_socket.unlink(missing_ok=True)
    host_events: list[dict[str, object]] = []
    _host_event(
        "MK_CONTRACT_QEMU_START",
        {
            "mode": config.mode,
            "kernel_sha": config.kernel_sha,
            "fixture_sha": config.fixture_sha,
            "log": config.log,
        },
        host_events,
    )
    process = await asyncio.create_subprocess_exec(
        config.qemu,
        *config.qemu_args(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    watchdog = ProgressWatchdog(config.idle_timeout_seconds)
    serial_task = asyncio.create_task(
        _stream_serial(process.stdout, config.log, watchdog)
    )
    qmp: QmpClient | None = None
    try:
        qmp = await QmpClient.connect(config.qmp_socket)
        await _wait_for_qemu(process, watchdog, config.timeout_seconds)
        await serial_task
    finally:
        if process.returncode is None:
            await _stop_qemu(process, qmp)
        if not serial_task.done():
            serial_task.cancel()
            await asyncio.gather(serial_task, return_exceptions=True)
        if qmp is not None:
            await qmp.close()
        config.qmp_socket.unlink(missing_ok=True)
    if process.returncode != 0:
        raise HarnessError(f"exit-status status={process.returncode}")
    serial_text = config.log.read_text(errors="replace")
    try:
        validate_contract_log(serial_text, config.mode)
    except ContractEvidenceError as error:
        raise HarnessError(str(error)) from error
    _host_event(
        "MK_CONTRACT_QEMU_PASS",
        {"mode": config.mode, "kernel_sha": config.kernel_sha},
        host_events,
    )
    count = _write_event_log(config.event_log, host_events, serial_text)
    print(
        f"MK_CONTRACT_EVENTS_OK mode={config.mode} "
        f"events={count} log={config.event_log}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("test",))
    parser.add_argument("mode", choices=MODES)
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        config = ContractConfig.from_environment(root, arguments.mode)
        config.verify_sources()
        return asyncio.run(_run_async(config))
    except HarnessError as error:
        print(
            f"MK_CONTRACT_QEMU_FAIL mode={arguments.mode} reason={error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
