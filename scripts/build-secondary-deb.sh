#!/usr/bin/env bash
set -euo pipefail

output_dir=${1:?usage: build-secondary-deb.sh OUTPUT_DIR VMLINUX KERNEL_RELEASE PACKAGE_VERSION TRACK FULL_SHA ARCH}
vmlinux=${2:?missing vmlinux}
kernel_release=${3:?missing kernel release}
package_version=${4:?missing package version}
track=${5:?missing track}
full_sha=${6:?missing full SHA}
architecture=${7:?missing architecture}

[[ "${track}" =~ ^[a-z0-9][a-z0-9.+-]{1,31}$ ]] || {
	printf 'invalid package track: %s\n' "${track}" >&2
	exit 1
}
[[ "${full_sha}" =~ ^[0-9a-f]{40}$ ]] || {
	printf 'invalid Linux commit SHA: %s\n' "${full_sha}" >&2
	exit 1
}
[[ -f "${vmlinux}" ]] || {
	printf 'secondary vmlinux does not exist: %s\n' "${vmlinux}" >&2
	exit 1
}

staging="${output_dir}/secondary-package-root"
case "${staging}" in
	*/secondary-package-root) ;;
	*) printf 'refusing unsafe package staging path: %s\n' "${staging}" >&2; exit 1 ;;
esac

package="linux-multikernel-secondary-${kernel_release}"
destination="${output_dir}/${package}_${package_version}_${architecture}.deb"
payload_dir="${staging}/usr/lib/multikernel/${kernel_release}"
doc_dir="${staging}/usr/share/doc/${package}"

rm -rf -- "${staging}"
mkdir -p "${staging}/DEBIAN" "${payload_dir}" "${doc_dir}" "${output_dir}"
install -m 0644 "${vmlinux}" "${payload_dir}/vmlinux"

cat >"${staging}/DEBIAN/control" <<EOF
Package: ${package}
Version: ${package_version}
Section: kernel
Priority: optional
Architecture: ${architecture}
Maintainer: Multikernel Build <multikernel@localhost.invalid>
Depends: linux-image-${kernel_release} (= ${package_version})
Description: Multikernel secondary ELF image for ${track}
 This package contains the ELF kernel image consumed by the Multikernel
 secondary loader. It is tied to one exact host kernel build and source commit.
EOF

cat >"${doc_dir}/build-manifest" <<EOF
track=${track}
kernel_release=${kernel_release}
package_version=${package_version}
linux_commit=${full_sha}
EOF

find "${staging}" -exec touch -h -d '@0' {} +
dpkg-deb --root-owner-group --build "${staging}" "${destination}" >/dev/null

actual_package=$(dpkg-deb -f "${destination}" Package)
actual_version=$(dpkg-deb -f "${destination}" Version)
[[ "${actual_package}" == "${package}" && "${actual_version}" == "${package_version}" ]] || {
	printf 'secondary package metadata validation failed\n' >&2
	exit 1
}

printf 'MK_SECONDARY_DEB_OK package=%s version=%s sha=%s output=%s\n' \
	"${package}" "${package_version}" "${full_sha}" "${destination}"
