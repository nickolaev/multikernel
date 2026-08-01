#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path


def digest(path: Path, algorithm: str) -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Write an APT Release file")
    parser.add_argument("distribution", type=Path)
    parser.add_argument("codename")
    parser.add_argument("architecture")
    args = parser.parse_args()
    distribution = args.distribution.resolve()
    if not distribution.is_dir():
        parser.error(f"distribution directory does not exist: {distribution}")

    excluded = {"Release", "Release.gpg", "InRelease"}
    files = sorted(
        path
        for path in distribution.rglob("*")
        if path.is_file() and path.name not in excluded
    )
    now = format_datetime(datetime.now(timezone.utc), usegmt=True)
    lines = [
        "Origin: Multikernel",
        "Label: Multikernel",
        f"Suite: {args.codename}",
        f"Codename: {args.codename}",
        f"Date: {now}",
        f"Architectures: {args.architecture}",
        "Components: main",
        "Description: Multikernel experimental Ubuntu packages",
    ]
    algorithms = (
        ("MD5Sum", "md5"),
        ("SHA1", "sha1"),
        ("SHA256", "sha256"),
        ("SHA512", "sha512"),
    )
    for heading, algorithm in algorithms:
        lines.append(f"{heading}:")
        for path in files:
            relative = path.relative_to(distribution).as_posix()
            lines.append(f" {digest(path, algorithm)} {path.stat().st_size:16d} {relative}")
    (distribution / "Release").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
