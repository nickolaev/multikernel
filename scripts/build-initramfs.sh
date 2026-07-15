#!/usr/bin/env bash
set -euo pipefail

mode=${1:?usage: build-initramfs.sh MODE OUTPUT BUSYBOX INIT ...}
output=${2:?missing output}
busybox=${3:?missing busybox}
init=${4:?missing init}
build_dir=$(cd "$(dirname "${output}")" && pwd)
root="${build_dir}/initramfs-${mode}"

case "${root}" in
	*/build/initramfs-host|*/build/initramfs-secondary) ;;
	*) printf 'refusing unsafe staging path: %s\n' "${root}" >&2; exit 1 ;;
esac

rm -rf -- "${root}"
mkdir -p "${root}/bin" "${root}/sbin" "${root}/dev" "${root}/proc" \
	"${root}/sys" "${root}/tmp" "${root}/run"
install -m 0755 "${busybox}" "${root}/bin/busybox"
install -m 0755 "${init}" "${root}/init"

for applet in sh mount mkdir cat grep sleep poweroff timeout sync; do
	ln -s busybox "${root}/bin/${applet}"
done

if [[ "${mode}" == host ]]; then
	[[ $# -eq 9 ]] || { printf 'host mode requires MKCTL SECONDARY_KERNEL SECONDARY_INITRD BASELINE_DTB INSTANCE_DTBO\n' >&2; exit 1; }
	mkctl=$5
	kernel=$6
	secondary_initrd=$7
	baseline_dtb=$8
	instance_dtbo=$9
	mkdir -p "${root}/assets" "${root}/payload"
	install -m 0755 "${mkctl}" "${root}/bin/mkctl"
	install -m 0644 "${kernel}" "${root}/payload/vmlinux"
	install -m 0644 "${secondary_initrd}" "${root}/payload/secondary-initrd.cpio.gz"
	install -m 0644 "${baseline_dtb}" "${root}/assets/baseline.dtb"
	install -m 0644 "${instance_dtbo}" "${root}/assets/instance.dtbo"
elif [[ "${mode}" != secondary ]]; then
	printf 'unknown initramfs mode: %s\n' "${mode}" >&2
	exit 1
fi

find "${root}" -exec touch -h -d '@0' {} +
mkdir -p "$(dirname "${output}")"
(
	cd "${root}"
	find . -print0 | LC_ALL=C sort -z |
		cpio --null -o --format=newc --owner=0:0 --reproducible 2>/dev/null |
		gzip -n -9 >"${output}"
)

printf 'MK_INITRD_OK mode=%s output=%s bytes=%s\n' \
	"${mode}" "${output}" "$(stat -c %s "${output}")"
