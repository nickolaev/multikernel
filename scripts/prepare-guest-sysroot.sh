#!/usr/bin/env bash
set -euo pipefail

destination=${1:?usage: prepare-guest-sysroot.sh DESTINATION}

case "${destination}" in
	*/build/guest-sysroot) ;;
	*) printf 'refusing unsafe guest sysroot path: %s\n' "${destination}" >&2; exit 1 ;;
esac

rm -rf -- "${destination}"
mkdir -p "$(dirname "${destination}")"
mmdebstrap --mode=chrootless --variant=extract --architectures=amd64 \
	--include=python3,python3-click,python3-libfdt \
	resolute "${destination}" \
	'deb http://archive.ubuntu.com/ubuntu resolute main universe'

file "${destination}/usr/bin/python3.14" | grep -q 'x86-64'
find "${destination}/usr/lib/python3/dist-packages" -name '_libfdt*.so' \
	-exec file {} \; | grep -q 'x86-64'
touch "${destination}/.ready"
printf 'MK_GUEST_SYSROOT_OK arch=x86_64 root=%s\n' "${destination}"
