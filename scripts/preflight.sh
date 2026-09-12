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
linux_head=$(git -C "${LINUX_DIR}" rev-parse --short=12 HEAD)
if [[ "${branch}" != mk-master && "${ALLOW_OTHER_BRANCH:-0}" != 1 ]]; then
	die "expected linux branch mk-master, found ${branch}; set ALLOW_OTHER_BRANCH=1 to override"
fi

for item in \
	"BUSYBOX:${BUSYBOX}" \
	"QEMU:${QEMU}" \
	"HOSTCC:${HOSTCC}" \
	"TARGET_CC:${TARGET_CC}" \
	"PYTHON:${PYTHON}" \
	"FLEX:${LEX}" \
	"BISON:${YACC}" \
	"MMDEBSTRAP:mmdebstrap" \
	"CPIO:cpio" \
	"GZIP:gzip"; do
	name=${item%%:*}
	value=${item#*:}
	[[ -n "${value}" ]] || die "required tool ${name} was not found"
	command -v "${value}" >/dev/null 2>&1 || die "required tool ${name} is not executable: ${value}"
done

[[ "${GUEST_ARCH}" == x86_64 ]] || die "unsupported guest architecture: ${GUEST_ARCH}"
[[ "${KERNEL_ARCH}" == x86 ]] || die "x86_64 guest requires KERNEL_ARCH=x86"
if [[ -n "${CROSS_COMPILE}" ]]; then
	command -v "${CROSS_COMPILE}gcc" >/dev/null 2>&1 || \
		die "cross compiler was not found: ${CROSS_COMPILE}gcc"
fi

"${PYTHON}" -m pip --version >/dev/null 2>&1 || die "required Python module pip was not found"

busybox_description=$(file "${BUSYBOX}")
grep -q 'statically linked' <<<"${busybox_description}" || die "BusyBox must be statically linked"
grep -q 'x86-64' <<<"${busybox_description}" || die "BusyBox must be an x86-64 guest binary"
if [[ ! -f /usr/include/gelf.h ]]; then
	command -v apt-get >/dev/null 2>&1 || die "apt-get is required to download libelf headers locally"
	command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb is required to extract local host dependencies"
fi

for value in "${QEMU_CPUS:-12}" "${QEMU_MEMORY_MB:-8192}" "${QEMU_TIMEOUT:-1200}" "${QEMU_IDLE_TIMEOUT:-120}"; do
	[[ "${value}" =~ ^[0-9]+$ ]] || die "QEMU numeric tunables must contain only digits"
done
(( ${QEMU_CPUS:-12} == 12 )) || die "QEMU_CPUS must be exactly 12"
(( ${QEMU_MEMORY_MB:-8192} == 8192 )) || die "QEMU_MEMORY_MB must be exactly 8192"
(( ${QEMU_TIMEOUT:-1200} >= 30 )) || die "QEMU_TIMEOUT must be at least 30 seconds"
(( ${QEMU_IDLE_TIMEOUT:-120} >= 1 )) || die "QEMU_IDLE_TIMEOUT must be at least 1 second"

printf 'MK_PREFLIGHT_OK branch=%s head=%s host_arch=%s guest_arch=%s hostcc=%s target_cc=%s flex=%s bison=%s python=%s busybox=%s qemu=%s\n' \
	"${branch}" "${linux_head}" "${HOST_ARCH}" "${GUEST_ARCH}" "${HOSTCC}" \
	"${TARGET_CC}" "${LEX}" "${YACC}" "${PYTHON}" "${BUSYBOX}" "${QEMU}"
