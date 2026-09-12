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
	[[ $# -eq 10 ]] || { printf 'host mode requires KERF_RUNTIME SECONDARY_KERNEL SECONDARY_INITRD LAZY_CMA_MODULE LAZY_CMA_TOOL HARNESS_PACKAGE\n' >&2; exit 1; }
	kerf_runtime=$5
	kernel=$6
	secondary_initrd=$7
	lazy_cma_module=$8
	lazy_cma_tool=$9
	harness_package=${10}
	cp -a "${kerf_runtime}/." "${root}/"
	mkdir -p "${root}/assets" "${root}/payload" "${root}/lib/modules"
	mkdir -p "${root}/usr/lib/python3/dist-packages/harness"
	cp -a "${harness_package}/." "${root}/usr/lib/python3/dist-packages/harness/"
	install -m 0644 "${kernel}" "${root}/payload/vmlinux"
	install -m 0644 "${secondary_initrd}" "${root}/payload/secondary-initrd.cpio.gz"
	install -m 0644 "${lazy_cma_module}" "${root}/lib/modules/lazy_cma.ko"
	install -m 0755 "${lazy_cma_tool}" "${root}/bin/lazy_cma_tool"
elif [[ "${mode}" == secondary ]]; then
	[[ $# -eq 6 ]] || { printf 'secondary mode requires PYTHON_RUNTIME HARNESS_PACKAGE\n' >&2; exit 1; }
	python_runtime=$5
	harness_package=$6
	cp -a "${python_runtime}/." "${root}/"
	mkdir -p "${root}/usr/lib/python3/dist-packages/harness"
	install -m 0644 "${harness_package}/__init__.py" \
		"${root}/usr/lib/python3/dist-packages/harness/__init__.py"
	install -m 0644 "${harness_package}/secondary.py" \
		"${root}/usr/lib/python3/dist-packages/harness/secondary.py"
	install -m 0644 "${harness_package}/events.py" \
		"${root}/usr/lib/python3/dist-packages/harness/events.py"
elif [[ "${mode}" != host ]]; then
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
