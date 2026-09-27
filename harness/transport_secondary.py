"""Numbered console producer for transport-only QEMU validation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
import time

from harness.events import encode_event, format_marker


def _instance_id(cmdline: str) -> int:
    for argument in cmdline.split():
        key, separator, value = argument.partition("=")
        if key == "mk_instance_id" and separator:
            return int(value)
    raise ValueError("mk_instance_id is missing")


def _open_console(path: Path, timeout: float = 60) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return os.open(path, os.O_WRONLY)
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("mktty console did not become available")


def _run_ring_test(instance: int, timeout: float = 90) -> None:
    if instance not in (1, 2):
        return
    role = "child" if instance == 1 else "peer"
    subprocess.run(
        [
            "/bin/busybox",
            "insmod",
            "/lib/modules/mk_ring_test.ko",
            f"role={role}",
        ],
        check=True,
    )
    result_path = Path("/sys/module/mk_ring_test/parameters/result")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = int(result_path.read_text().strip())
        if value == 1:
            return
        if value < 0:
            raise RuntimeError(f"ring test failed: {value}")
        time.sleep(0.05)
    raise RuntimeError("ring test timed out")


def _emit(fd: int, event: str, fields: dict[str, object]) -> None:
    payload = (
        encode_event(event, fields, "secondary")
        + "\n"
        + format_marker(event, fields)
        + "\n"
    )
    os.write(fd, payload.encode("ascii", errors="replace"))


def main() -> int:
    instance = _instance_id(Path("/proc/cmdline").read_text())
    fd = _open_console(Path("/dev/mktty0"))
    try:
        _run_ring_test(instance)
        _emit(fd, "MK_TRANSPORT_READY", {"instance": instance})
        sequence = 0
        while True:
            _emit(
                fd,
                "MK_TRANSPORT_SEQUENCE",
                {"instance": instance, "sequence": sequence},
            )
            sequence += 1
            time.sleep(0.01)
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
