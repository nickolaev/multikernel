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
memory_mb=${QEMU_MEMORY_MB:-6144}
timeout_seconds=${QEMU_TIMEOUT:-300}

[[ "${mode}" == run || "${mode}" == test ]] || { printf 'unknown mode: %s\n' "${mode}" >&2; exit 1; }
[[ "${cpus}" =~ ^[0-9]+$ && "${memory_mb}" =~ ^[0-9]+$ && "${timeout_seconds}" =~ ^[0-9]+$ ]] || {
	printf 'QEMU tunables must be numeric\n' >&2
	exit 1
}
(( cpus >= 3 )) || { printf 'QEMU_CPUS must be at least 3\n' >&2; exit 1; }
(( memory_mb >= 5120 )) || { printf 'QEMU_MEMORY_MB must be at least 5120\n' >&2; exit 1; }

args=(
	-machine q35,accel=tcg
	-cpu max
	-smp "${cpus}"
	-m "${memory_mb}"
	-device intel-iommu,intremap=on
	-kernel "${kernel}"
	-initrd "${initrd}"
	-append 'console=ttyS0,115200 rdinit=/init panic=-1 kho=on intel_iommu=on iommu.strict=1'
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
	'MK_STAGE_IOMMU_ENABLED'
	'MK_STAGE_IOMMU_GROUP'
	'MK_STAGE_VF_CREATED'
	'MK_STAGE_PF_RETAINED'
	'MK_STAGE_VF_HOST_OWNED'
	'MK_STAGE_KERF_INIT_OK'
	'MK_STAGE_KERF_CREATE_OK'
	'MK_STAGE_STATUS_ready'
	'MK_STAGE_VF_LEASED'
	'MK_STAGE_IOMMU_DOMAIN'
	'MK_STAGE_VF_ASSIGNED'
	'MK_STAGE_KERF_LOAD_OK'
	'MK_STAGE_STATUS_loaded'
	'MK_STAGE_MKTTY_CONNECTED'
	'MK_STAGE_KERF_EXEC_OK'
	'MK_STAGE_STATUS_active'
	'MK_STAGE_PF_LINK_UP pf=0000:00:02.0'
	'MK_STAGE_PF_ADDRESS pf=0000:00:02.0'
	'MK_SECONDARY_VF_ENUMERATED instance=1 bdf=0000:00:12.0 vendor=0x8086 device=0x10ca'
	'MK_SECONDARY_VF_BAR index=0'
	'MK_SECONDARY_PF_ABSENT instance=1 bdf=0000:00:02.0'
	'MK_SECONDARY_PCI_ISOLATED instance=1 devices=1 vf=0000:00:12.0'
	'MK_SECONDARY_VF_READY instance=1 bdf=0000:00:12.0 driver=igbvf netdev='
	'MK_SECONDARY_VF_TRAFFIC_BEFORE netdev='
	'MK_SECONDARY_VF_DATAPATH netdev='
	'MK_SECONDARY_PRIMARY_REACHABLE netdev='
	'MK_SECONDARY_ALIVE'
	'MK_PRIMARY_STILL_ALIVE'
	'MK_STAGE_PF_ACTIVE pf=0000:00:02.0 driver=igb'
	'MK_STAGE_KERF_KILL_OK'
	'MK_STAGE_STATUS_loaded'
	'MK_STAGE_KERF_UNLOAD_OK'
	'MK_STAGE_STATUS_ready'
	'MK_STAGE_KERF_DELETE_OK'
	'MK_STAGE_VF_RESTORED'
	'MK_HOSTILE_CREATE_REJECTED stage=pf-assignment'
	'MK_HOSTILE_CREATE_REJECTED stage=duplicate-vf'
	'MK_HOSTILE_CREATE_REJECTED stage=second-owner'
	'MK_HOSTILE_VF_DISABLE_REJECTED phase=ready'
	'MK_HOSTILE_VF_REBIND_REJECTED phase=ready'
	'MK_HOSTILE_VF_REPROBE_CONTAINED phase=ready'
	'MK_HOSTILE_PF_BIND_REJECTED phase=ready'
	'MK_HOSTILE_VF_DISABLE_REJECTED phase=active'
	'MK_HOSTILE_VF_REBIND_REJECTED phase=active'
	'MK_HOSTILE_VF_REPROBE_CONTAINED phase=active'
	'MK_HOSTILE_PF_BIND_REJECTED phase=active'
	'MK_HOSTILE_LEASE_PERSISTED phase=after-kill'
	'MK_HOSTILE_LEASE_PERSISTED phase=after-unload'
	'MK_STAGE_VF_RESTORED phase=cycle-1'
	'MK_REPEAT_CYCLE_PASS cycle=2'
	'MK_REPEAT_CYCLE_PASS cycle=3'
	'MK_REPEAT_CYCLE_PASS cycle=4'
	'MK_HOSTILE_SURPRISE_UNBIND_FAIL_CLOSED id=104'
	'MK_HOSTILE_SURPRISE_UNBIND_RECOVERED id=104'
	'MK_STAGE_VF_TEARDOWN pf=0000:00:02.0 vfs=0'
	'MK_DEMO_PASS simultaneous_kernels=verified'
)

if grep -Eq 'MK_DEMO_FAIL|MK_SECONDARY_FAIL' "${log}"; then
	printf 'MK_QEMU_FAIL reason=guest-failure-marker log=%s\n' "${log}" >&2
	exit 1
fi

for marker in "${markers[@]}"; do
	grep -Fq "${marker}" "${log}" || {
		printf 'MK_QEMU_FAIL reason=missing-marker marker=%q log=%s\n' "${marker}" "${log}" >&2
		exit 1
	}
done
printf 'MK_QEMU_TEST_PASS markers=%d log=%s\n' "${#markers[@]}" "${log}"
