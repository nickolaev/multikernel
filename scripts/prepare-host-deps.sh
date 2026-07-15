#!/usr/bin/env bash
set -euo pipefail

destination=${1:?usage: prepare-host-deps.sh DESTINATION}
cache="${destination}/cache"
root="${destination}/root"

mkdir -p "${cache}" "${root}"
if [[ ! -f "${root}/usr/include/gelf.h" ]]; then
	(
		cd "${cache}"
		apt-get download libelf-dev
	)
	while IFS= read -r -d '' package; do
		dpkg-deb -x "${package}" "${root}"
	done < <(find "${cache}" -maxdepth 1 -type f -name '*.deb' -print0)
fi

[[ -f "${root}/usr/include/gelf.h" ]] || {
	printf 'host dependency bootstrap: gelf.h was not extracted\n' >&2
	exit 1
}
system_libelf=$(ldconfig -p | awk '/libelf\.so\.1 \(/{print $NF; exit}')
[[ -n "${system_libelf}" && -f "${system_libelf}" ]] || {
	printf 'host dependency bootstrap: system libelf.so.1 was not found\n' >&2
	exit 1
}
ln -sfn "${system_libelf}" "${root}/usr/lib/x86_64-linux-gnu/libelf.so"
[[ -e "${root}/usr/lib/x86_64-linux-gnu/libelf.so" ]] || {
	printf 'host dependency bootstrap: libelf.so was not extracted\n' >&2
	exit 1
}

touch "${root}/.ready"
printf 'MK_HOST_DEPS_OK root=%s\n' "${root}"
