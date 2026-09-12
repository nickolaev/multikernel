#!/usr/bin/env bash
set -euo pipefail

destination=${1:?usage: prepare-guest-busybox.sh DESTINATION}
package_url=https://archive.ubuntu.com/ubuntu/pool/main/b/busybox/busybox-static_1.37.0-7ubuntu1_amd64.deb
package_sha256=fd605342f62268753076aa7d9321ff098b36ba53e47434145a9aedc28fd141a4
package="${destination}/busybox-static_amd64.deb"
package_root="${destination}/busybox-package"
output="${destination}/busybox-x86_64"

mkdir -p "${destination}"
if [[ ! -f "${package}" ]] || ! echo "${package_sha256}  ${package}" | sha256sum -c - >/dev/null 2>&1; then
	rm -f -- "${package}"
	curl -fL --retry 3 -o "${package}" "${package_url}"
fi
echo "${package_sha256}  ${package}" | sha256sum -c -

rm -rf -- "${package_root}"
dpkg-deb -x "${package}" "${package_root}"
install -m 0755 "${package_root}/usr/bin/busybox" "${output}"
description=$(file "${output}")
grep -q 'x86-64' <<<"${description}"
grep -q 'statically linked' <<<"${description}"
printf 'MK_GUEST_BUSYBOX_OK path=%s\n' "${output}"
