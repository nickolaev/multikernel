#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${root}"
exec "${PYTHON:-python3}" -m harness.qemu "$@"
