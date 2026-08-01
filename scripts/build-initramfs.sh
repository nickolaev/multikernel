#!/usr/bin/env bash
set -euo pipefail

mode=${1:?usage: build-initramfs.sh MODE OUTPUT BUSYBOX INIT ...}
output=${2:?missing output}
busybox=${3:?missing busybox}
init=${4:?missing init}
build_dir=$(cd "$(dirname "${output}")" && pwd)
root="${build_dir}/initramfs-${mode}"

case "${root}" in
	*/build/initramfs-host|*/build/initramfs-secondary|\
	*/build/package-test/initramfs-host|*/build/package-test/initramfs-secondary) ;;
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
	[[ $# -eq 11 ]] || { printf 'host mode requires KERF_RUNTIME SECONDARY_KERNEL SECONDARY_INITRD LAZY_CMA_MODULE LAZY_CMA_TOOL HARNESS_PACKAGE QEMU_BINARY\n' >&2; exit 1; }
	kerf_runtime=$5
	kernel=$6
	secondary_initrd=$7
	lazy_cma_module=$8
	lazy_cma_tool=$9
	harness_package=${10}
	qemu_binary=${11}
	mkdir -p "${root}/assets" "${root}/payload" "${root}/lib/modules" "${root}/usr/bin"
	cp -a "${kerf_runtime}/." "${root}/"
	mkdir -p "${root}/usr/lib/python3/dist-packages/harness"
	cp -a "${harness_package}/." "${root}/usr/lib/python3/dist-packages/harness/"
	install -m 0644 "${kernel}" "${root}/payload/vmlinux"
	install -m 0644 "${secondary_initrd}" "${root}/payload/secondary-initrd.cpio.gz"
	install -m 0644 "${lazy_cma_module}" "${root}/lib/modules/lazy_cma.ko"
	install -m 0755 "${lazy_cma_tool}" "${root}/bin/lazy_cma_tool"
	install -m 0755 "${qemu_binary}" "${root}/usr/bin/qemu-system-x86_64"
	while IFS= read -r library; do
		install -D -m 0755 "${library}" "${root}${library}"
	done < <(ldd "${qemu_binary}" | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\//) { print $i; break } }')
elif [[ "${mode}" == secondary ]]; then
	[[ $# -eq 6 ]] || { printf 'secondary mode requires PYTHON_RUNTIME HARNESS_PACKAGE\n' >&2; exit 1; }
	python_runtime=$5
	harness_package=$6
	python_binary="${python_runtime}/usr/bin/python3"
	python_stdlib=$(find "${python_runtime}/usr/lib" -maxdepth 1 -type d -name 'python3.*' -print -quit)
	[[ -n "${python_stdlib}" ]] || { printf 'Python standard library not found in %s\n' "${python_runtime}" >&2; exit 1; }
	python_version=${python_stdlib##*/}
	mkdir -p "${root}/usr/bin" "${root}/usr/lib/${python_version}"
	install -m 0755 "${python_binary}" "${root}/usr/bin/python3"
	while IFS= read -r library; do
		install -D -m 0755 "${library}" "${root}${library}"
	done < <(ldd "${python_binary}" | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\//) { print $i; break } }')
	for entry in __future__.py _collections_abc.py _py_warnings.py _weakrefset.py \
		abc.py codecs.py collections contextlib.py copyreg.py encodings enum.py \
		fnmatch.py functools.py genericpath.py glob.py importlib keyword.py \
		json linecache.py locale.py operator.py os.py pathlib posixpath.py re reprlib.py \
		selectors.py signal.py stat.py subprocess.py threading.py types.py \
		warnings.py zipimport.py io.py ntpath.py; do
		cp -a "${python_stdlib}/${entry}" "${root}/usr/lib/${python_version}/"
	done
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
