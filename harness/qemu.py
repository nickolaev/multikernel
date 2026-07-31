"""Build and run the single-VF QEMU integration scenario."""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


REQUIRED_MARKERS = (
    "contains BAR 0 for 8 VFs",
    "Intel(R) Gigabit Ethernet Network Connection",
    "MK_STAGE_POOL_OK",
    "MK_STAGE_IOMMU_ENABLED",
    "MK_STAGE_IOMMU_GROUP",
    "MK_STAGE_VF_CREATED",
    "MK_STAGE_PF_RETAINED",
    "MK_STAGE_VF_HOST_OWNED",
    "MK_STAGE_KERF_INIT_OK",
    "MK_STAGE_KERF_CREATE_OK",
    "MK_STAGE_STATUS_ready",
    "MK_STAGE_VF_LEASED",
    "MK_STAGE_IOMMU_DOMAIN",
    "MK_STAGE_VF_ASSIGNED",
    "MK_STAGE_KERF_LOAD_OK",
    "MK_STAGE_STATUS_loaded",
    "MK_STAGE_MKTTY_CONNECTED",
    "MK_STAGE_KERF_EXEC_OK",
    "MK_STAGE_STATUS_active",
    "MK_STAGE_PF_LINK_UP pf=0000:00:02.0",
    "MK_STAGE_PF_ADDRESS pf=0000:00:02.0",
    "MK_SECONDARY_VF_ENUMERATED instance=1 bdf=0000:00:12.0 vendor=0x8086 device=0x10ca",
    "MK_SECONDARY_VF_BAR index=0",
    "MK_SECONDARY_PF_ABSENT instance=1 bdf=0000:00:02.0",
    "MK_SECONDARY_PCI_ISOLATED instance=1 devices=1 vf=0000:00:12.0",
    "MK_SECONDARY_VF_READY instance=1 bdf=0000:00:12.0 driver=igbvf netdev=",
    "MK_SECONDARY_VF_TRAFFIC_BEFORE netdev=",
    "MK_SECONDARY_VF_DATAPATH netdev=",
    "MK_SECONDARY_PRIMARY_REACHABLE netdev=",
    "MK_SECONDARY_ALIVE",
    "MK_PRIMARY_STILL_ALIVE",
    "MK_STAGE_PF_ACTIVE pf=0000:00:02.0 driver=igb",
    "MK_STAGE_KERF_KILL_OK",
    "MK_STAGE_KERF_UNLOAD_OK",
    "MK_STAGE_KERF_DELETE_OK",
    "MK_STAGE_VF_RESTORED",
    "MK_HOSTILE_CREATE_REJECTED stage=pf-assignment",
    "MK_HOSTILE_CREATE_REJECTED stage=duplicate-vf",
    "MK_HOSTILE_CREATE_REJECTED stage=second-owner",
    "MK_HOSTILE_VF_DISABLE_REJECTED phase=ready",
    "MK_HOSTILE_VF_REBIND_REJECTED phase=ready",
    "MK_HOSTILE_VF_REPROBE_CONTAINED phase=ready",
    "MK_HOSTILE_PF_BIND_REJECTED phase=ready",
    "MK_HOSTILE_VF_DISABLE_REJECTED phase=active",
    "MK_HOSTILE_VF_REBIND_REJECTED phase=active",
    "MK_HOSTILE_VF_REPROBE_CONTAINED phase=active",
    "MK_HOSTILE_PF_BIND_REJECTED phase=active",
    "MK_HOSTILE_LEASE_PERSISTED phase=after-kill",
    "MK_HOSTILE_LEASE_PERSISTED phase=after-unload",
    "MK_STAGE_VF_RESTORED phase=cycle-1",
    "MK_REPEAT_CYCLE_PASS cycle=2",
    "MK_REPEAT_CYCLE_PASS cycle=3",
    "MK_REPEAT_CYCLE_PASS cycle=4",
    "MK_HOSTILE_SURPRISE_UNBIND_FAIL_CLOSED id=104",
    "MK_HOSTILE_SURPRISE_UNBIND_RECOVERED id=104",
    "MK_STAGE_VF_TEARDOWN pf=0000:00:02.0 vfs=0",
    "MK_DEMO_PASS simultaneous_kernels=verified",
)
FAILURE_MARKERS = ("MK_DEMO_FAIL", "MK_SECONDARY_FAIL")


class HarnessError(RuntimeError):
    """A deterministic harness configuration or verification failure."""


def _numeric_setting(environ: Mapping[str, str], name: str, default: int) -> int:
    value = environ.get(name, str(default))
    if not value.isdigit():
        raise HarnessError("QEMU tunables must be numeric")
    return int(value)


@dataclass(frozen=True)
class HarnessConfig:
    root: Path
    build_dir: Path
    qemu: str
    cpus: int
    memory_mb: int
    timeout_seconds: int

    @classmethod
    def from_environment(
        cls, root: Path, environ: Mapping[str, str] = os.environ
    ) -> "HarnessConfig":
        build_dir = Path(environ.get("BUILD_DIR", root / "build"))
        config = cls(
            root=root,
            build_dir=build_dir,
            qemu=environ.get("QEMU", "qemu-system-x86_64"),
            cpus=_numeric_setting(environ, "QEMU_CPUS", 4),
            memory_mb=_numeric_setting(environ, "QEMU_MEMORY_MB", 6144),
            timeout_seconds=_numeric_setting(environ, "QEMU_TIMEOUT", 300),
        )
        config.validate()
        return config

    @property
    def kernel(self) -> Path:
        return self.build_dir / "kernel/arch/x86/boot/bzImage"

    @property
    def initrd(self) -> Path:
        return self.build_dir / "host-initrd.cpio.gz"

    @property
    def log(self) -> Path:
        return self.build_dir / "qemu-serial.log"

    def validate(self) -> None:
        if self.cpus < 3:
            raise HarnessError("QEMU_CPUS must be at least 3")
        if self.memory_mb < 5120:
            raise HarnessError("QEMU_MEMORY_MB must be at least 5120")

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
            "-device",
            "intel-iommu,intremap=on",
            "-kernel",
            str(self.kernel),
            "-initrd",
            str(self.initrd),
            "-append",
            "console=ttyS0,115200 rdinit=/init panic=-1 kho=on intel_iommu=on iommu.strict=1",
            "-nographic",
            "-monitor",
            "none",
            "-no-reboot",
            "-netdev",
            "user,id=net0",
            "-device",
            "igb,netdev=net0",
        ]


def validate_log(log_text: str) -> None:
    for marker in FAILURE_MARKERS:
        if marker in log_text:
            raise HarnessError("guest-failure-marker")
    for marker in REQUIRED_MARKERS:
        if marker not in log_text:
            raise HarnessError(f"missing-marker marker={marker!r}")


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_test(config: HarnessConfig) -> int:
    config.build_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"MK_QEMU_START timeout={config.timeout_seconds}s log={config.log}",
        flush=True,
    )
    process = subprocess.Popen(
        [config.qemu, *config.qemu_args()],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + config.timeout_seconds
    timed_out = False
    with config.log.open("wb") as log_file:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process(process)
                break
            readable, _, _ = select.select(
                [process.stdout], [], [], min(remaining, 0.5)
            )
            if readable:
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if chunk:
                    log_file.write(chunk)
                    log_file.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            if process.poll() is not None:
                remainder = process.stdout.read()
                if remainder:
                    log_file.write(remainder)
                    sys.stdout.buffer.write(remainder)
                    sys.stdout.buffer.flush()
                break

    if timed_out:
        raise HarnessError("timeout")
    if process.returncode != 0:
        raise HarnessError(f"exit-status status={process.returncode}")
    validate_log(config.log.read_text(errors="replace"))
    print(f"MK_QEMU_TEST_PASS markers={len(REQUIRED_MARKERS)} log={config.log}")
    return 0


def run(mode: str, config: HarnessConfig) -> int:
    if mode == "run":
        os.execvpe(config.qemu, [config.qemu, *config.qemu_args()], os.environ)
        raise AssertionError("execvpe returned")
    return _run_test(config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "test"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        return run(args.mode, HarnessConfig.from_environment(root))
    except HarnessError as error:
        build_dir = Path(os.environ.get("BUILD_DIR", root / "build"))
        log = build_dir / "qemu-serial.log"
        print(f"MK_QEMU_FAIL reason={error} log={log}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
