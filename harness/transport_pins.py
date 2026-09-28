"""Fail-closed source identity checks for transport QEMU evidence."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Sequence


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class SourcePinError(RuntimeError):
    """A source checkout does not match the requested immutable identity."""


@dataclass(frozen=True)
class SourcePin:
    label: str
    path: Path
    expected_sha: str


def _git(path: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SourcePinError(
            f"git-execution-failed source={path} operation={arguments[0]}"
        ) from error
    if result.returncode:
        raise SourcePinError(f"git-failed source={path} operation={arguments[0]}")
    return result.stdout.strip()


def verify_source_pin(pin: SourcePin) -> str:
    """Return the exact SHA after checking absolute path, identity, and cleanliness."""
    if not pin.path.is_absolute():
        raise SourcePinError(f"relative-source-path label={pin.label}")
    if not SHA_PATTERN.fullmatch(pin.expected_sha):
        raise SourcePinError(f"invalid-expected-sha label={pin.label}")
    actual = _git(pin.path, "rev-parse", "HEAD")
    if actual != pin.expected_sha:
        raise SourcePinError(
            f"source-sha-mismatch label={pin.label} "
            f"expected={pin.expected_sha} actual={actual}"
        )
    status = _git(pin.path, "status", "--porcelain", "--untracked-files=normal")
    if status:
        raise SourcePinError(f"dirty-source label={pin.label}")
    return actual


def verify_source_pins(pins: Sequence[SourcePin]) -> None:
    for pin in pins:
        verify_source_pin(pin)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for label in ("fixture", "linux", "kerf", "lazy-cma"):
        key = label.replace("-", "_")
        parser.add_argument(f"--{label}-dir", dest=f"{key}_dir", required=True)
        parser.add_argument(f"--{label}-sha", dest=f"{key}_sha", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    pins = tuple(
        SourcePin(
            label,
            Path(getattr(args, f"{label.replace('-', '_')}_dir")),
            getattr(args, f"{label.replace('-', '_')}_sha"),
        )
        for label in ("fixture", "linux", "kerf", "lazy-cma")
    )
    try:
        verify_source_pins(pins)
    except SourcePinError as error:
        print(f"MK_TRANSPORT_PINS_FAIL reason={error}")
        return 1
    print(
        "MK_TRANSPORT_PINS_OK "
        + " ".join(
            f"{pin.label}_path={pin.path} {pin.label}_sha={pin.expected_sha}"
            for pin in pins
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
