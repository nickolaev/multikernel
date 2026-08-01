#!/usr/bin/env bash
set -euo pipefail

output_dir=${1:?usage: build-meta-deb.sh OUTPUT_DIR PACKAGE PACKAGE_VERSION KERNEL_RELEASE KERNEL_VERSION INITRAMFS_PACKAGE TRACK}
package=${2:?missing package name}
package_version=${3:?missing package version}
kernel_release=${4:?missing kernel release}
kernel_version=${5:?missing kernel package version}
initramfs_package=${6:?missing initramfs package}
track=${7:?missing track}
[[ "${package}" =~ ^[a-z0-9][a-z0-9+.-]+$ ]] || { printf 'invalid metapackage name: %s\n' "${package}" >&2; exit 1; }

staging="${output_dir}/meta-package-root"
destination="${output_dir}/${package}_${package_version}_all.deb"
doc_dir="${staging}/usr/share/doc/${package}"
case "${staging}" in
	*/meta-package-root) ;;
	*) printf 'refusing unsafe package staging path: %s\n' "${staging}" >&2; exit 1 ;;
esac
depends="linux-image-${kernel_release} (= ${kernel_version}), linux-multikernel-secondary-${kernel_release} (= ${kernel_version}), ${initramfs_package} (= ${package_version})"

rm -rf -- "${staging}"
mkdir -p "${staging}/DEBIAN" "${doc_dir}" "${output_dir}"
cat >"${staging}/DEBIAN/control" <<EOF
Package: ${package}
Version: ${package_version}
Section: metapackages
Priority: optional
Architecture: all
Maintainer: Multikernel Build <multikernel@localhost.invalid>
Depends: ${depends}
Description: Install the matched Multikernel ${track} boot artifact set
 This metapackage installs one exactly matched Linux image, secondary ELF,
 and host/secondary initramfs package.
EOF
cat >"${doc_dir}/build-manifest" <<EOF
track=${track}
kernel_release=${kernel_release}
package_version=${package_version}
kernel_package_version=${kernel_version}
EOF
find "${staging}" -exec touch -h -d '@0' {} +
SOURCE_DATE_EPOCH=0 dpkg-deb --root-owner-group --build "${staging}" "${destination}" >/dev/null

actual_package=$(dpkg-deb -f "${destination}" Package)
actual_version=$(dpkg-deb -f "${destination}" Version)
actual_depends=$(dpkg-deb -f "${destination}" Depends)
[[ "${actual_package}" == "${package}" && "${actual_version}" == "${package_version}" ]] || {
	printf 'metapackage metadata validation failed\n' >&2
	exit 1
}
[[ "${actual_depends}" == "${depends}" ]] || {
	printf 'metapackage dependency validation failed: %s\n' "${actual_depends}" >&2
	exit 1
}
printf 'MK_META_DEB_OK package=%s version=%s output=%s\n' \
	"${package}" "${package_version}" "${destination}"
