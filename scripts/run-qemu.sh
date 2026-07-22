#!/usr/bin/env bash
set -euo pipefail

mode=${1:?usage: run-qemu.sh run|test}
platform=${PLATFORM:?PLATFORM is required}
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
build_dir=${BUILD_DIR:-"${root}/build/${platform}"}
kernel=${KERNEL:?KERNEL is required}
initrd="${build_dir}/host-initrd.cpio.gz"
log="${build_dir}/qemu-serial.log"
qemu=${QEMU:?QEMU is required}
machine=${QEMU_MACHINE:?QEMU_MACHINE is required}
cpu=${QEMU_CPU:?QEMU_CPU is required}
append=${QEMU_APPEND:?QEMU_APPEND is required}
cpus=${QEMU_CPUS:-4}
memory_mb=${QEMU_MEMORY_MB:-2048}
timeout_seconds=${QEMU_TIMEOUT:-180}

[[ "${mode}" == run || "${mode}" == test ]] || { printf 'unknown mode: %s\n' "${mode}" >&2; exit 1; }
[[ "${platform}" == x86 || "${platform}" == riscv ]] || { printf 'unknown platform: %s\n' "${platform}" >&2; exit 1; }
[[ "${cpus}" =~ ^[0-9]+$ && "${memory_mb}" =~ ^[0-9]+$ && "${timeout_seconds}" =~ ^[0-9]+$ ]] || {
	printf 'QEMU tunables must be numeric\n' >&2
	exit 1
}
(( cpus >= 3 )) || { printf 'QEMU_CPUS must be at least 3\n' >&2; exit 1; }
(( memory_mb >= 1536 )) || { printf 'QEMU_MEMORY_MB must be at least 1536\n' >&2; exit 1; }
[[ -f "${kernel}" ]] || { printf 'kernel not found: %s\n' "${kernel}" >&2; exit 1; }
[[ -f "${initrd}" ]] || { printf 'initrd not found: %s\n' "${initrd}" >&2; exit 1; }

args=(
	-machine "${machine}"
	-cpu "${cpu}"
	-smp "${cpus}"
	-m "${memory_mb}"
	-kernel "${kernel}"
	-initrd "${initrd}"
	-append "${append}"
	-nographic
	-monitor none
	-no-reboot
)

if [[ "${mode}" == run ]]; then
	exec "${qemu}" "${args[@]}"
fi

mkdir -p "${build_dir}"
: >"${log}"
printf 'MK_QEMU_START platform=%s timeout=%ss kernel=%s log=%s\n' "${platform}" "${timeout_seconds}" "${kernel}" "${log}"
set +e
timeout --foreground "${timeout_seconds}" "${qemu}" "${args[@]}" 2>&1 | tee "${log}"
qemu_status=${PIPESTATUS[0]}
set -e

if (( qemu_status == 124 )); then
	printf 'MK_QEMU_FAIL reason=timeout log=%s\n' "${log}" >&2
	exit 1
fi
if (( qemu_status != 0 )); then
	printf 'MK_QEMU_FAIL reason=exit-status status=%d log=%s\n' "${qemu_status}" "${log}" >&2
	exit 1
fi

markers=(
	'MK_STAGE_POOL_OK'
	'MK_STAGE_KERF_INIT_OK'
	'MK_STAGE_KERF_CREATE_OK'
	'MK_STAGE_STATUS_ready'
	'MK_STAGE_KERF_LOAD_OK'
	'MK_STAGE_STATUS_loaded'
	'MK_STAGE_KERF_EXEC_OK'
	'MK_STAGE_STATUS_active'
	'MK_SECONDARY_ALIVE instance=1'
	'MK_PRIMARY_STILL_ALIVE'
)

last_line=0
for marker in "${markers[@]}"; do
	line=$(grep -n -F -m1 "${marker}" "${log}" | cut -d: -f1 || true)
	[[ -n "${line}" ]] || {
		printf 'MK_QEMU_FAIL reason=missing-marker marker=%q log=%s\n' "${marker}" "${log}" >&2
		exit 1
	}
	(( line > last_line )) || {
		printf 'MK_QEMU_FAIL reason=marker-order marker=%q line=%d previous=%d log=%s\n' \
			"${marker}" "${line}" "${last_line}" "${log}" >&2
		exit 1
	}
	last_line=${line}
done

if grep -Eq 'Kernel panic|Oops:|illegal instruction|MK_(DEMO|SECONDARY)_FAIL' "${log}"; then
	printf 'MK_QEMU_FAIL reason=fatal-guest-output log=%s\n' "${log}" >&2
	exit 1
fi

printf 'MK_DEMO_PASS simultaneous_kernels=verified transport=uart\n'
printf 'MK_QEMU_TEST_PASS platform=%s markers=%d log=%s\n' "${platform}" "${#markers[@]}" "${log}"
