#!/usr/bin/env bash
set -euo pipefail

config=${1:?usage: check-config.sh CONFIG}
platform=${PLATFORM:?PLATFORM is required}

common=(
	CONFIG_64BIT CONFIG_SMP CONFIG_HOTPLUG_CPU CONFIG_RELOCATABLE
	CONFIG_SPARSEMEM CONFIG_MEMORY_HOTPLUG CONFIG_MEMORY_HOTREMOVE
	CONFIG_GENERIC_ALLOCATOR CONFIG_PRINTK CONFIG_TTY
	CONFIG_BLK_DEV_INITRD CONFIG_RD_GZIP CONFIG_BINFMT_ELF CONFIG_BINFMT_SCRIPT
	CONFIG_DEVTMPFS CONFIG_DEVTMPFS_MOUNT CONFIG_PROC_FS CONFIG_SYSFS CONFIG_TMPFS
	CONFIG_KEXEC CONFIG_KEXEC_FILE CONFIG_KEXEC_HANDOVER
	CONFIG_KEXEC_HANDOVER_ENABLE_DEFAULT CONFIG_MULTIKERNEL CONFIG_MKTTY
)

case "${platform}" in
	x86)
		required=(CONFIG_X86_64 CONFIG_PCI CONFIG_X86_MCE CONFIG_SERIAL_8250 CONFIG_SERIAL_8250_CONSOLE)
		;;
	riscv)
		required=(CONFIG_RISCV CONFIG_MMU CONFIG_RISCV_SBI CONFIG_SERIAL_8250 CONFIG_SERIAL_8250_CONSOLE)
		;;
	*) printf 'config check: unsupported PLATFORM %s\n' "${platform}" >&2; exit 1 ;;
esac

required+=("${common[@]}")
for symbol in "${required[@]}"; do
	grep -qx "${symbol}=y" "${config}" || {
		printf 'config check: required %s=y is missing for PLATFORM=%s\n' "${symbol}" "${platform}" >&2
		exit 1
	}
done

printf 'MK_CONFIG_OK platform=%s required_symbols=%d\n' "${platform}" "${#required[@]}"
