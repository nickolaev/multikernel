"""Minimal child-side readiness agent for boot-contract validation."""

from __future__ import annotations

import os
from pathlib import Path
import time

from harness.events import encode_event, format_marker


def _instance_id() -> int:
    for argument in Path("/proc/cmdline").read_text().split():
        key, separator, value = argument.partition("=")
        if key == "mk_instance_id" and separator:
            return int(value)
    raise RuntimeError("mk_instance_id is missing")


def main() -> int:
    instance = _instance_id()
    fd = os.open("/dev/mktty0", os.O_WRONLY)
    try:
        fields = {"instance": instance}
        payload = (
            encode_event("MK_CONTRACT_CHILD_READY", fields, "secondary")
            + "\n"
            + format_marker("MK_CONTRACT_CHILD_READY", fields)
            + "\n"
        )
        os.write(fd, payload.encode("ascii"))
        while True:
            time.sleep(60)
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
