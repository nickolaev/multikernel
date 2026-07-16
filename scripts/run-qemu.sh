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
	-netdev user,id=net0
	-device igb,netdev=net0
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
	'contains BAR 0 for 8 VFs'
	'Intel(R) Gigabit Ethernet Network Connection'
	'MK_STAGE_POOL_OK'
	'MK_STAGE_VF_CREATED'
	'MK_STAGE_PF_RETAINED'
	'MK_STAGE_KERF_INIT_OK'
	'MK_STAGE_KERF_CREATE_OK'
	'MK_STAGE_STATUS_ready'
	'MK_STAGE_VF_ASSIGNED'
	'MK_STAGE_KERF_LOAD_OK'
	'MK_STAGE_STATUS_loaded'
	'MK_STAGE_MKTTY_CONNECTED'
	'MK_STAGE_KERF_EXEC_OK'
	'MK_STAGE_STATUS_active'
	'MK_STAGE_PF_LINK_UP pf=0000:00:02.0'
	'MK_SECONDARY_ECAM_CONFIG_READ bdf=0000:00:12.0 value=0xffffffff'
	'MK_SECONDARY_ECAM_CLASS_READ bdf=0000:00:12.0 value='
	'MK_SECONDARY_ASSIGNED_PCI_IDENTITY bdf=0000:00:12.0 vendor=8086 device=10ca'
	'MK_SECONDARY_VF_ENUMERATED instance=1 bdf=0000:00:12.0 vendor=0x8086 device=0x10ca'
	'MK_SECONDARY_VF_BAR index=0'
	'MK_SECONDARY_PF_ABSENT instance=1 bdf=0000:00:02.0'
	'MK_SECONDARY_VF_READY instance=1 bdf=0000:00:12.0 driver=igbvf netdev='
	'MK_SECONDARY_VF_TRAFFIC_BEFORE netdev='
	'MK_SECONDARY_VF_DATAPATH netdev='
	'MK_SECONDARY_ALIVE'
	'MK_PRIMARY_STILL_ALIVE'
	'MK_STAGE_PF_ACTIVE pf=0000:00:02.0 driver=igb'
	'MK_STAGE_KERF_KILL_OK'
	'MK_STAGE_VF_TEARDOWN pf=0000:00:02.0 vfs=0'
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
