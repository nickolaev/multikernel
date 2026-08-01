#!/usr/bin/env bash
set -euo pipefail

output_dir=${1:?usage: build-initramfs-deb.sh OUTPUT_DIR HOST_INITRD SECONDARY_INITRD KERNEL_RELEASE PACKAGE_VERSION KERNEL_VERSION TRACK LINUX_SHA KERF_SHA QEMU_SHA LAZY_CMA_SHA HARNESS_SHA ARCH}
host_initrd=${2:?missing host initramfs}
secondary_initrd=${3:?missing secondary initramfs}
kernel_release=${4:?missing kernel release}
package_version=${5:?missing package version}
kernel_version=${6:?missing kernel package version}
track=${7:?missing track}
linux_sha=${8:?missing Linux SHA}
kerf_sha=${9:?missing Kerf SHA}
qemu_sha=${10:?missing QEMU SHA}
lazy_cma_sha=${11:?missing lazy-CMA SHA}
harness_sha=${12:?missing harness SHA}
architecture=${13:?missing architecture}

for sha in "${linux_sha}" "${kerf_sha}" "${qemu_sha}" "${lazy_cma_sha}" "${harness_sha}"; do
	[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || { printf 'invalid source SHA: %s\n' "${sha}" >&2; exit 1; }
done
for artifact in "${host_initrd}" "${secondary_initrd}"; do
	[[ -f "${artifact}" ]] || { printf 'initramfs artifact does not exist: %s\n' "${artifact}" >&2; exit 1; }
done
[[ "${track}" =~ ^[a-z0-9][a-z0-9.+-]{1,31}$ ]] || { printf 'invalid package track: %s\n' "${track}" >&2; exit 1; }

package="multikernel-initramfs-${kernel_release}"
staging="${output_dir}/initramfs-package-root"
destination="${output_dir}/${package}_${package_version}_${architecture}.deb"
payload_dir="${staging}/usr/lib/multikernel/${kernel_release}"
doc_dir="${staging}/usr/share/doc/${package}"
case "${staging}" in
	*/initramfs-package-root) ;;
	*) printf 'refusing unsafe package staging path: %s\n' "${staging}" >&2; exit 1 ;;
esac

rm -rf -- "${staging}"
mkdir -p "${staging}/DEBIAN" "${payload_dir}" "${doc_dir}" "${output_dir}"
install -m 0644 "${host_initrd}" "${payload_dir}/host-initrd.cpio.gz"
install -m 0644 "${secondary_initrd}" "${payload_dir}/secondary-initrd.cpio.gz"
cat >"${staging}/DEBIAN/control" <<EOF
Package: ${package}
Version: ${package_version}
Section: kernel
Priority: optional
Architecture: ${architecture}
Maintainer: Multikernel Build <multikernel@localhost.invalid>
Depends: linux-image-${kernel_release} (= ${kernel_version}), linux-multikernel-secondary-${kernel_release} (= ${kernel_version})
Description: Multikernel host and secondary initramfs images for ${track}
 This package contains the matched host and secondary initramfs images,
 including the Kerf runtime used by the Multikernel boot harness.
EOF
cat >"${doc_dir}/build-manifest" <<EOF
track=${track}
kernel_release=${kernel_release}
package_version=${package_version}
kernel_package_version=${kernel_version}
linux_commit=${linux_sha}
kerf_commit=${kerf_sha}
qemu_commit=${qemu_sha}
lazy_cma_commit=${lazy_cma_sha}
harness_commit=${harness_sha}
EOF
find "${staging}" -exec touch -h -d '@0' {} +
SOURCE_DATE_EPOCH=0 dpkg-deb --root-owner-group --build "${staging}" "${destination}" >/dev/null

actual_package=$(dpkg-deb -f "${destination}" Package)
actual_version=$(dpkg-deb -f "${destination}" Version)
actual_depends=$(dpkg-deb -f "${destination}" Depends)
expected_depends="linux-image-${kernel_release} (= ${kernel_version}), linux-multikernel-secondary-${kernel_release} (= ${kernel_version})"
[[ "${actual_package}" == "${package}" && "${actual_version}" == "${package_version}" ]] || {
	printf 'initramfs package metadata validation failed\n' >&2
	exit 1
}
[[ "${actual_depends}" == "${expected_depends}" ]] || {
	printf 'initramfs package dependency validation failed: %s\n' "${actual_depends}" >&2
	exit 1
}
printf 'MK_INITRAMFS_DEB_OK package=%s version=%s linux_sha=%s kerf_sha=%s qemu_sha=%s lazy_cma_sha=%s harness_sha=%s output=%s\n' \
	"${package}" "${package_version}" "${linux_sha}" "${kerf_sha}" "${qemu_sha}" \
	"${lazy_cma_sha}" "${harness_sha}" "${destination}"
