#!/usr/bin/env bash
set -euo pipefail

die() {
	printf 'preflight: %s\n' "$*" >&2
	exit 1
}

platform=${PLATFORM:?PLATFORM is required}
case "${platform}" in
	x86)
		expected_arch=x86
		expected_qemu=qemu-system-x86_64
		;;
	riscv)
		expected_arch=riscv
		expected_qemu=qemu-system-riscv64
		;;
	*) die "unsupported PLATFORM ${platform}; expected x86 or riscv" ;;
esac

[[ "${KARCH:?KARCH is required}" == "${expected_arch}" ]] ||
	die "PLATFORM ${platform} requires ARCH ${expected_arch}, found ${KARCH}"
[[ -d "${LINUX_DIR}/.git" ]] || die "${LINUX_DIR} is not a Linux Git tree"
[[ -f "${LINUX_DIR}/kernel/multikernel/Kconfig" ]] || die "Multikernel sources are missing"
[[ -f "${KERF_DIR}/src/kerf/cli.py" ]] || die "${KERF_DIR} is not a Kerf source tree"

linux_branch=$(git -C "${LINUX_DIR}" branch --show-current)
kerf_branch=$(git -C "${KERF_DIR}" branch --show-current)
if [[ "${linux_branch}" != riscv && "${ALLOW_OTHER_BRANCH:-0}" != 1 ]]; then
	die "expected linux branch riscv, found ${linux_branch}; set ALLOW_OTHER_BRANCH=1 to override"
fi
if [[ "${kerf_branch}" != riscv && "${ALLOW_OTHER_BRANCH:-0}" != 1 ]]; then
	die "expected kerf branch riscv, found ${kerf_branch}; set ALLOW_OTHER_BRANCH=1 to override"
fi

for item in \
	"BUSYBOX:${BUSYBOX}" \
	"QEMU:${QEMU}" \
	"CC:${CC}" \
	"PYTHON:${PYTHON}" \
	"FLEX:${LEX}" \
	"BISON:${YACC}" \
	"CPIO:cpio" \
	"GZIP:gzip" \
	"FILE:file"; do
	name=${item%%:*}
	value=${item#*:}
	[[ -n "${value}" ]] || die "required tool ${name} was not found"
	command -v "${value}" >/dev/null 2>&1 || die "required tool ${name} is not executable: ${value}"
done

[[ "$(basename "${QEMU}")" == "${expected_qemu}" ]] ||
	die "PLATFORM ${platform} requires ${expected_qemu}, found ${QEMU}"
"${PYTHON}" -m pip --version >/dev/null 2>&1 || die "required Python module pip was not found"
file "${BUSYBOX}" | grep -q 'statically linked' || die "BusyBox must be statically linked"

if [[ ! -f /usr/include/gelf.h ]]; then
	command -v apt-get >/dev/null 2>&1 || die "apt-get is required to download libelf headers locally"
	command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb is required to extract local host dependencies"
fi

if [[ "${platform}" == riscv ]]; then
	cross_compile=${CROSS_COMPILE:-riscv64-linux-gnu-}
	for base in gcc ld ar nm objcopy objdump readelf; do
		tool=${cross_compile}${base}
		command -v "${tool}" >/dev/null 2>&1 ||
			die "PLATFORM riscv requires ${tool}; install crossbuild-essential-riscv64"
	done
fi

for value in "${QEMU_CPUS:-4}" "${QEMU_MEMORY_MB:-2048}" "${QEMU_TIMEOUT:-180}"; do
	[[ "${value}" =~ ^[0-9]+$ ]] || die "QEMU numeric tunables must contain only digits"
done
(( ${QEMU_CPUS:-4} >= 3 )) || die "QEMU_CPUS must be at least 3 (instance uses CPU/hart 2)"
(( ${QEMU_MEMORY_MB:-2048} >= 1536 )) || die "QEMU_MEMORY_MB must be at least 1536 for the fixed pool"
(( ${QEMU_TIMEOUT:-180} >= 30 )) || die "QEMU_TIMEOUT must be at least 30 seconds"

printf 'MK_PREFLIGHT_OK platform=%s linux_branch=%s kerf_branch=%s arch=%s cc=%s python=%s busybox=%s qemu=%s\n' \
	"${platform}" "${linux_branch}" "${kerf_branch}" "${KARCH}" "${CC}" "${PYTHON}" "${BUSYBOX}" "${QEMU}"
