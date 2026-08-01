#!/usr/bin/env bash
set -euo pipefail

missing=()
for command_name in dpkg-buildpackage dpkg-deb dpkg-checkbuilddeps dh_listpackages; do
	command -v "${command_name}" >/dev/null 2>&1 || missing+=("command:${command_name}")
done
for header in /usr/include/gelf.h /usr/include/elfutils/libdw.h; do
	[[ -f "${header}" ]] || missing+=("file:${header}")
done

if (( ${#missing[@]} )); then
	printf 'missing Debian kernel build dependencies:\n' >&2
	printf '  %s\n' "${missing[@]}" >&2
	printf 'install them with: sudo apt install debhelper libdw-dev libelf-dev\n' >&2
	exit 1
fi

for package_name in debhelper libdw-dev libelf-dev; do
	dpkg-query -W -f='${db:Status-Abbrev}\n' "${package_name}" 2>/dev/null | grep -q '^ii ' || {
		printf 'required package is not installed: %s\n' "${package_name}" >&2
		exit 1
	}
done

printf 'MK_DEB_BUILD_DEPS_OK debhelper=%s libdw=%s libelf=%s\n' \
	"$(dpkg-query -W -f='${Version}' debhelper)" \
	"$(dpkg-query -W -f='${Version}' libdw-dev)" \
	"$(dpkg-query -W -f='${Version}' libelf-dev)"
