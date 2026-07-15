#!/usr/bin/env bash
set -euo pipefail

mode=${1:?usage: run-qemu.sh run|test}
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
build_dir=${BUILD_DIR:-"${root}/build"}
kernel="${build_dir}/kernel/arch/x86/boot/bzImage"
initrd="${build_dir}/host-initrd.cpio.gz"
log="${build_dir}/qemu-serial.log"
qemu=${QEMU:-qemu-system-x86_64}
cpus=${QEMU_CPUS:-4}
memory_mb=${QEMU_MEMORY_MB:-2048}
timeout_seconds=${QEMU_TIMEOUT:-180}

[[ "${mode}" == run || "${mode}" == test ]] || { printf 'unknown mode: %s\n' "${mode}" >&2; exit 1; }
[[ "${cpus}" =~ ^[0-9]+$ && "${memory_mb}" =~ ^[0-9]+$ && "${timeout_seconds}" =~ ^[0-9]+$ ]] || {
	printf 'QEMU tunables must be numeric\n' >&2
	exit 1
}
(( cpus >= 3 )) || { printf 'QEMU_CPUS must be at least 3\n' >&2; exit 1; }
(( memory_mb >= 1536 )) || { printf 'QEMU_MEMORY_MB must be at least 1536\n' >&2; exit 1; }

args=(
	-machine q35,accel=tcg
	-cpu max
	-smp "${cpus}"
	-m "${memory_mb}"
	-kernel "${kernel}"
	-initrd "${initrd}"
	-append 'console=ttyS0,115200 rdinit=/init panic=-1 mkkernel_pool=512M@0x40000000 kho=on'
	-nographic
	-monitor none
	-no-reboot
)

if [[ "${mode}" == run ]]; then
	exec "${qemu}" "${args[@]}"
fi

mkdir -p "${build_dir}"
: >"${log}"
printf 'MK_QEMU_START timeout=%ss log=%s\n' "${timeout_seconds}" "${log}"
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
	'MK_STAGE_BASELINE_OK'
	'MK_STAGE_STATUS_ready'
	'MK_STAGE_LOAD_SYSCALL_OK'
	'MK_STAGE_STATUS_loaded'
	'MK_STAGE_MKTTY_CONNECTED'
	'MK_STAGE_EXEC_SYSCALL_OK'
	'MK_STAGE_STATUS_active'
	'MK_SECONDARY_ALIVE'
	'MK_PRIMARY_STILL_ALIVE'
	'MK_DEMO_PASS simultaneous_kernels=verified'
)

for marker in "${markers[@]}"; do
	grep -Fq "${marker}" "${log}" || {
		printf 'MK_QEMU_FAIL reason=missing-marker marker=%q log=%s\n' "${marker}" "${log}" >&2
		exit 1
	}
done
if grep -Fq 'MK_DEMO_FAIL' "${log}"; then
	printf 'MK_QEMU_FAIL reason=guest-failure-marker log=%s\n' "${log}" >&2
	exit 1
fi

printf 'MK_QEMU_TEST_PASS markers=%d log=%s\n' "${#markers[@]}" "${log}"
