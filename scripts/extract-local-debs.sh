#!/usr/bin/env bash
set -euo pipefail

image_deb=${1:?usage: extract-local-debs.sh IMAGE_DEB SECONDARY_DEB INITRAMFS_DEB PACKAGE_ROOT HARNESS_BUILD_DIR KERNEL_RELEASE}
secondary_deb=${2:?missing secondary package}
initramfs_deb=${3:?missing initramfs package}
package_root=${4:?missing package root}
harness_build_dir=${5:?missing harness build directory}
kernel_release=${6:?missing kernel release}

for package_path in "${image_deb}" "${secondary_deb}" "${initramfs_deb}"; do
	[[ -f "${package_path}" ]] || {
		printf 'Debian package does not exist: %s\n' "${package_path}" >&2
		exit 1
	}
done
case "${package_root}" in
	*/package-root) ;;
	*) printf 'refusing unsafe extraction root: %s\n' "${package_root}" >&2; exit 1 ;;
esac
case "${harness_build_dir}" in
	*/package-test) ;;
	*) printf 'refusing unsafe harness build directory: %s\n' "${harness_build_dir}" >&2; exit 1 ;;
esac

rm -rf -- "${package_root}" "${harness_build_dir}"
mkdir -p "${package_root}" "${harness_build_dir}/kernel/arch/x86/boot"
dpkg-deb -x "${image_deb}" "${package_root}"
dpkg-deb -x "${secondary_deb}" "${package_root}"
dpkg-deb -x "${initramfs_deb}" "${package_root}"

packaged_kernel="${package_root}/boot/vmlinuz-${kernel_release}"
packaged_secondary="${package_root}/usr/lib/multikernel/${kernel_release}/vmlinux"
packaged_host_initrd="${package_root}/usr/lib/multikernel/${kernel_release}/host-initrd.cpio.gz"
packaged_secondary_initrd="${package_root}/usr/lib/multikernel/${kernel_release}/secondary-initrd.cpio.gz"
for packaged_path in "${packaged_kernel}" "${packaged_secondary}" \
		"${packaged_host_initrd}" "${packaged_secondary_initrd}"; do
	[[ -f "${packaged_path}" ]] || { printf 'package payload is missing %s\n' "${packaged_path}" >&2; exit 1; }
done

install -m 0644 "${packaged_kernel}" "${harness_build_dir}/kernel/arch/x86/boot/bzImage"
install -m 0644 "${packaged_secondary}" "${harness_build_dir}/kernel/vmlinux"
install -m 0644 "${packaged_host_initrd}" "${harness_build_dir}/host-initrd.cpio.gz"
install -m 0644 "${packaged_secondary_initrd}" "${harness_build_dir}/secondary-initrd.cpio.gz"
cmp -s "${packaged_kernel}" "${harness_build_dir}/kernel/arch/x86/boot/bzImage"
cmp -s "${packaged_secondary}" "${harness_build_dir}/kernel/vmlinux"
cmp -s "${packaged_host_initrd}" "${harness_build_dir}/host-initrd.cpio.gz"
cmp -s "${packaged_secondary_initrd}" "${harness_build_dir}/secondary-initrd.cpio.gz"

printf 'MK_DEB_EXTRACT_OK kernel_release=%s image=%s secondary=%s initramfs=packaged root=%s\n' \
	"${kernel_release}" "${harness_build_dir}/kernel/arch/x86/boot/bzImage" \
	"${harness_build_dir}/kernel/vmlinux" "${package_root}"
