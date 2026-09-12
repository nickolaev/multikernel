#!/usr/bin/env bash
set -euo pipefail

kerf_dir=${1:?usage: prepare-kerf-runtime.sh KERF_DIR BUILD_DIR GUEST_SYSROOT TARGET_CC PYTHON}
build_dir=${2:?missing build directory}
guest_sysroot=${3:?missing guest sysroot}
target_cc=${4:?missing target compiler}
python=${5:?missing host Python interpreter}
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime="${build_dir}/kerf-runtime"
packages="${build_dir}/kerf-packages"
site_dir="${runtime}/usr/lib/python3/dist-packages"
rdtsc_sha256=993a8fe80ad00e3cca976feaa0a4fe98978e95e013ab5d668aeed7890bde4e8b

case "${runtime}" in
	*/build/kerf-runtime) ;;
	*) printf 'refusing unsafe runtime path: %s\n' "${runtime}" >&2; exit 1 ;;
esac
[[ -f "${guest_sysroot}/.ready" ]] || {
	printf 'guest sysroot is not ready: %s\n' "${guest_sysroot}" >&2
	exit 1
}

rm -rf -- "${runtime}" "${packages}"
mkdir -p "${runtime}" "${packages}" "${site_dir}"
cp -a "${guest_sysroot}/." "${runtime}/"
ln -sfn usr/lib "${runtime}/lib"
ln -sfn usr/lib64 "${runtime}/lib64"
rm -rf -- "${runtime}/var/lib/apt/lists" "${runtime}/var/cache/apt" \
	"${runtime}/usr/share/doc" "${runtime}/usr/share/man"

cp -a "${kerf_dir}/src/kerf" "${site_dir}/kerf"
(
	cd "${packages}"
	"${python}" -m pip download --disable-pip-version-check --no-deps \
		--no-binary=:all: 'rdtsc==0.2.1'
)
rdtsc_archive="${packages}/rdtsc-0.2.1.tar.gz"
echo "${rdtsc_sha256}  ${rdtsc_archive}" | sha256sum -c -
tar -xzf "${rdtsc_archive}" -C "${packages}"
mkdir -p "${site_dir}/rdtsc"
"${target_cc}" -shared -fPIC -O2 \
	-o "${site_dir}/rdtsc/rdtsc.so.1" "${packages}/rdtsc-0.2.1/src/rdtsc.c"
install -m 0644 "${root}/scripts/rdtsc-init.py" "${site_dir}/rdtsc/__init__.py"

python_binary=$(readlink -f "${runtime}/usr/bin/python3")
[[ "${python_binary}" == "${runtime}"/* ]] || {
	printf 'guest Python resolves outside runtime: %s\n' "${python_binary}" >&2
	exit 1
}

while IFS= read -r -d '' candidate; do
	description=$(file "${candidate}")
	if grep -q 'ELF ' <<<"${description}" && ! grep -q 'x86-64' <<<"${description}"; then
		printf 'non-x86 guest ELF: %s: %s\n' "${candidate}" "${description}" >&2
		exit 1
	fi
done < <(find "${runtime}" -type f -print0)

[[ -f "${site_dir}/click/__init__.py" ]] || { printf 'Click is missing from guest runtime\n' >&2; exit 1; }
find "${site_dir}" -name '_libfdt*.so' -print -quit | grep -q . || {
	printf 'libfdt Python extension is missing from guest runtime\n' >&2
	exit 1
}
touch "${runtime}/.ready"
printf 'MK_KERF_RUNTIME_OK guest_arch=x86_64 bytes=%s\n' \
	"$(du -sb "${runtime}" | awk '{print $1}')"
