#!/usr/bin/env bash
set -euo pipefail

destination=${1:?usage: prepare-qemu-deps.sh DESTINATION}
cache="${destination}/cache"
root="${destination}/root"
packages=(
	libffi-dev
	libgio-2.0-dev
	libglib2.0-dev
	libmount-dev
	libpcre2-dev
	libpixman-1-dev
	libselinux-dev
	libsysprof-capture-4-dev
	ninja-build
	zlib1g-dev
)

mkdir -p "${cache}" "${root}"
if [[ ! -x "${root}/usr/bin/ninja" ||
      ! -f "${root}/usr/lib/x86_64-linux-gnu/pkgconfig/glib-2.0.pc" ||
      ! -f "${root}/usr/lib/x86_64-linux-gnu/pkgconfig/pixman-1.pc" ]]; then
	(
		cd "${cache}"
		apt-get download "${packages[@]}"
	)
	while IFS= read -r -d '' package; do
		dpkg-deb -x "${package}" "${root}"
	done < <(find "${cache}" -maxdepth 1 -type f -name '*.deb' -print0)
fi

require_file() {
	local path=$1
	[[ -e "${path}" ]] || {
		printf 'qemu dependency bootstrap: missing %s\n' "${path}" >&2
		exit 1
	}
}

link_system_library() {
	local soname=$1
	local destination_name=$2
	local system_library
	local library_dir="${root}/usr/lib/x86_64-linux-gnu"

	system_library=$(ldconfig -p | awk -v soname="${soname}" '
		index($1, soname) == 1 && !path { path = $NF }
		END { print path }
	')
	[[ -n "${system_library}" && -f "${system_library}" ]] || {
		printf 'qemu dependency bootstrap: system %s was not found\n' "${soname}" >&2
		exit 1
	}
	mkdir -p "${library_dir}"
	ln -sfn "${system_library}" "${library_dir}/${destination_name}"
}

link_system_library libglib-2.0.so.0 libglib-2.0.so.0
link_system_library libpixman-1.so.0 libpixman-1.so.0

require_file "${root}/usr/bin/ninja"
require_file "${root}/usr/include/glib-2.0/glib.h"
require_file "${root}/usr/include/pixman-1/pixman.h"
require_file "${root}/usr/lib/x86_64-linux-gnu/pkgconfig/glib-2.0.pc"
require_file "${root}/usr/lib/x86_64-linux-gnu/pkgconfig/pixman-1.pc"
require_file "${root}/usr/lib/x86_64-linux-gnu/libglib-2.0.so"
require_file "${root}/usr/lib/x86_64-linux-gnu/libpixman-1.so"

touch "${root}/.ready"
printf 'MK_QEMU_DEPS_OK root=%s\n' "${root}"
