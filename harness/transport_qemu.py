"""Run the transport-only three-child QEMU scenario."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping, Sequence

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
from harness.transport import SHA_PATTERN, TransportEvidenceError, validate_transport_log
from harness.transport_pins import SourcePin, SourcePinError, verify_source_pins


def _numeric(environ: Mapping[str, str], name: str, default: int) -> int:
    value = environ.get(name, str(default))
    if not value.isdigit():
        raise HarnessError(f"{name} must be numeric")
    return int(value)


@dataclass(frozen=True)
class TransportConfig:
    root: Path
    build_dir: Path
    qemu: str
    cpus: int
    memory_mb: int
    timeout_seconds: int
    idle_timeout_seconds: int
    kernel_sha: str
    fixture_sha: str
    kerf_sha: str
    lazy_cma_sha: str
    linux_dir: Path
    fixture_dir: Path
    kerf_dir: Path
    lazy_cma_dir: Path

    @classmethod
    def from_environment(
        cls, root: Path, environ: Mapping[str, str] = os.environ
    ) -> "TransportConfig":
        config = cls(
            root=root,
            build_dir=Path(environ.get("BUILD_DIR", root / "build")),
            qemu=environ.get("QEMU", "qemu-system-x86_64"),
            cpus=_numeric(environ, "QEMU_CPUS", 12),
            memory_mb=_numeric(environ, "QEMU_MEMORY_MB", 8192),
            timeout_seconds=_numeric(environ, "QEMU_TIMEOUT", 900),
            idle_timeout_seconds=_numeric(environ, "QEMU_IDLE_TIMEOUT", 120),
            kernel_sha=environ.get("TRANSPORT_KERNEL_SHA", ""),
            fixture_sha=environ.get("TRANSPORT_FIXTURE_SHA", ""),
            kerf_sha=environ.get("TRANSPORT_KERF_SHA", ""),
            lazy_cma_sha=environ.get("TRANSPORT_LAZY_CMA_SHA", ""),
            linux_dir=Path(environ.get("TRANSPORT_LINUX_DIR", "")),
            fixture_dir=Path(environ.get("TRANSPORT_FIXTURE_DIR", "")),
            kerf_dir=Path(environ.get("TRANSPORT_KERF_DIR", "")),
            lazy_cma_dir=Path(environ.get("TRANSPORT_LAZY_CMA_DIR", "")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.cpus < 5:
            raise HarnessError("transport QEMU requires at least 5 CPUs")
        if self.memory_mb < 2048:
            raise HarnessError("transport QEMU requires at least 2048 MiB")
        if self.timeout_seconds < 30 or self.idle_timeout_seconds < 1:
            raise HarnessError("invalid transport timeout")
        for name, value in (
            ("TRANSPORT_KERNEL_SHA", self.kernel_sha),
            ("TRANSPORT_FIXTURE_SHA", self.fixture_sha),
            ("TRANSPORT_KERF_SHA", self.kerf_sha),
            ("TRANSPORT_LAZY_CMA_SHA", self.lazy_cma_sha),
        ):
            if not SHA_PATTERN.fullmatch(value):
                raise HarnessError(f"{name} must be a full 40-character SHA")

    def verify_sources(self) -> None:
        try:
            verify_source_pins(
                (
                    SourcePin("fixture", self.fixture_dir, self.fixture_sha),
                    SourcePin("linux", self.linux_dir, self.kernel_sha),
                    SourcePin("kerf", self.kerf_dir, self.kerf_sha),
                    SourcePin("lazy-cma", self.lazy_cma_dir, self.lazy_cma_sha),
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
        return self.build_dir / f"transport-qemu-serial-{self.kernel_sha}.log"

    @property
    def event_log(self) -> Path:
        return self.build_dir / f"transport-qemu-events-{self.kernel_sha}.jsonl"

    @property
    def qmp_socket(self) -> Path:
        return self.build_dir / "transport-qemu-qmp.sock"

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
            f"mk_transport_test=1 mk_transport_kernel_sha={self.kernel_sha} "
            f"mk_transport_fixture_sha={self.fixture_sha}",
            "-nographic",
            "-monitor",
            "none",
            "-qmp",
            f"unix:{self.qmp_socket},server=on,wait=off",
            "-no-reboot",
        ]


async def _run_async(config: TransportConfig) -> int:
    config.build_dir.mkdir(parents=True, exist_ok=True)
    config.qmp_socket.unlink(missing_ok=True)
    host_events: list[dict[str, object]] = []
    _host_event(
        "MK_TRANSPORT_SOURCE_PINS_VERIFIED",
        {
            "kernel_sha": config.kernel_sha,
            "fixture_sha": config.fixture_sha,
            "kerf_sha": config.kerf_sha,
            "lazy_cma_sha": config.lazy_cma_sha,
            "linux_dir": config.linux_dir,
            "fixture_dir": config.fixture_dir,
            "kerf_dir": config.kerf_dir,
            "lazy_cma_dir": config.lazy_cma_dir,
        },
        host_events,
    )
    _host_event(
        "MK_TRANSPORT_QEMU_START",
        {
            "kernel_sha": config.kernel_sha,
            "fixture_sha": config.fixture_sha,
            "kerf_sha": config.kerf_sha,
            "lazy_cma_sha": config.lazy_cma_sha,
            "linux_dir": config.linux_dir,
            "fixture_dir": config.fixture_dir,
            "kerf_dir": config.kerf_dir,
            "lazy_cma_dir": config.lazy_cma_dir,
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
    serial_task = asyncio.create_task(_stream_serial(process.stdout, config.log, watchdog))
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
        validate_transport_log(serial_text)
    except TransportEvidenceError as error:
        raise HarnessError(str(error)) from error
    _host_event(
        "MK_TRANSPORT_QEMU_PASS",
        {"kernel_sha": config.kernel_sha, "fixture_sha": config.fixture_sha},
        host_events,
    )
    count = _write_event_log(config.event_log, host_events, serial_text)
    print(f"MK_TRANSPORT_EVENTS_OK events={count} log={config.event_log}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("test",))
    parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        config = TransportConfig.from_environment(root)
        config.verify_sources()
        return asyncio.run(_run_async(config))
    except HarnessError as error:
        print(f"MK_TRANSPORT_QEMU_FAIL reason={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
