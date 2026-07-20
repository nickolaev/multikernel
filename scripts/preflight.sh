#!/usr/bin/env bash
set -euo pipefail

die() {
	printf 'preflight: %s\n' "$*" >&2
	exit 1
}

[[ -d "${LINUX_DIR}/.git" ]] || die "${LINUX_DIR} is not a Linux Git tree"
[[ -f "${LINUX_DIR}/kernel/multikernel/Kconfig" ]] || die "Multikernel sources are missing"
[[ -f "${KERF_DIR}/src/kerf/cli.py" ]] || die "${KERF_DIR} is not a Kerf source tree"
[[ -f "${LAZY_CMA_DIR}/lazy_cma.c" ]] || die "${LAZY_CMA_DIR} is not a lazy_cma source tree"

branch=$(git -C "${LINUX_DIR}" branch --show-current)
if [[ "${branch}" != mk-master && "${ALLOW_OTHER_BRANCH:-0}" != 1 ]]; then
	die "expected linux branch mk-master, found ${branch}; set ALLOW_OTHER_BRANCH=1 to override"
fi

for item in \
	"BUSYBOX:${BUSYBOX}" \
	"QEMU:${QEMU}" \
	"CC:${CC}" \
	"PYTHON:${PYTHON}" \
	"FLEX:${LEX}" \
	"BISON:${YACC}" \
	"CPIO:cpio" \
	"GZIP:gzip"; do
	name=${item%%:*}
	value=${item#*:}
	[[ -n "${value}" ]] || die "required tool ${name} was not found"
	command -v "${value}" >/dev/null 2>&1 || die "required tool ${name} is not executable: ${value}"
done

"${PYTHON}" -m pip --version >/dev/null 2>&1 || die "required Python module pip was not found"

file "${BUSYBOX}" | grep -q 'statically linked' || die "BusyBox must be statically linked"
if [[ ! -f /usr/include/gelf.h ]]; then
	command -v apt-get >/dev/null 2>&1 || die "apt-get is required to download libelf headers locally"
	command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb is required to extract local host dependencies"
fi

for value in "${QEMU_CPUS:-4}" "${QEMU_MEMORY_MB:-6144}" "${QEMU_TIMEOUT:-180}"; do
	[[ "${value}" =~ ^[0-9]+$ ]] || die "QEMU numeric tunables must contain only digits"
done
(( ${QEMU_CPUS:-4} >= 3 )) || die "QEMU_CPUS must be at least 3 (instance uses CPU 2)"
(( ${QEMU_MEMORY_MB:-6144} >= 5120 )) || die "QEMU_MEMORY_MB must be at least 5120 for lazy_cma to allocate from ZONE_NORMAL"
(( ${QEMU_TIMEOUT:-180} >= 30 )) || die "QEMU_TIMEOUT must be at least 30 seconds"

printf 'MK_PREFLIGHT_OK branch=%s cc=%s flex=%s bison=%s python=%s busybox=%s qemu=%s\n' \
	"${branch}" "${CC}" "${LEX}" "${YACC}" "${PYTHON}" "${BUSYBOX}" "${QEMU}"
