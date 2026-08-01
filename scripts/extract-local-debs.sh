#!/usr/bin/env bash
set -euo pipefail

image_deb=${1:?usage: extract-local-debs.sh IMAGE_DEB SECONDARY_DEB PACKAGE_ROOT HARNESS_BUILD_DIR KERNEL_RELEASE}
secondary_deb=${2:?missing secondary package}
package_root=${3:?missing package root}
harness_build_dir=${4:?missing harness build directory}
kernel_release=${5:?missing kernel release}

for package_path in "${image_deb}" "${secondary_deb}"; do
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

packaged_kernel="${package_root}/boot/vmlinuz-${kernel_release}"
packaged_secondary="${package_root}/usr/lib/multikernel/${kernel_release}/vmlinux"
[[ -f "${packaged_kernel}" ]] || {
	printf 'kernel package is missing %s\n' "${packaged_kernel}" >&2
	exit 1
}
[[ -f "${packaged_secondary}" ]] || {
	printf 'secondary package is missing %s\n' "${packaged_secondary}" >&2
	exit 1
}

install -m 0644 "${packaged_kernel}" "${harness_build_dir}/kernel/arch/x86/boot/bzImage"
install -m 0644 "${packaged_secondary}" "${harness_build_dir}/kernel/vmlinux"
cmp -s "${packaged_kernel}" "${harness_build_dir}/kernel/arch/x86/boot/bzImage"
cmp -s "${packaged_secondary}" "${harness_build_dir}/kernel/vmlinux"

printf 'MK_DEB_EXTRACT_OK kernel_release=%s image=%s secondary=%s root=%s\n' \
	"${kernel_release}" "${harness_build_dir}/kernel/arch/x86/boot/bzImage" \
	"${harness_build_dir}/kernel/vmlinux" "${package_root}"
