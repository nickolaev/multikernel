#!/usr/bin/env bash
set -euo pipefail

kerf_dir=${1:?usage: prepare-riscv-runtime.sh KERF_DIR BUILD_DIR}
build_dir=${2:?missing build directory}
release=${RISCV_UBUNTU_RELEASE:-resolute}
runtime="${build_dir}/kerf-runtime"
apt_root="${build_dir}/riscv-apt"
archives="${apt_root}/cache/archives"
sources="${apt_root}/sources.list"

case "${runtime}" in
	*/build/riscv/kerf-runtime) ;;
	*) printf 'refusing unsafe RISC-V runtime path: %s\n' "${runtime}" >&2; exit 1 ;;
esac

rm -rf -- "${runtime}" "${apt_root}"
mkdir -p "${runtime}" "${archives}/partial" \
	"${apt_root}/state/lists/partial"
: >"${apt_root}/state/status"
printf 'deb [arch=riscv64 trusted=yes] https://ports.ubuntu.com/ubuntu-ports %s main universe\n' \
	"${release}" >"${sources}"

apt_options=(
	-o Debug::NoLocking=1
	-o "Dir::Etc::sourcelist=${sources}"
	-o Dir::Etc::sourceparts=-
	-o APT::Architecture=riscv64
	-o APT::Architectures::=riscv64
	-o "Dir::State=${apt_root}/state"
	-o "Dir::State::status=${apt_root}/state/status"
	-o "Dir::Cache=${apt_root}/cache"
)

apt-get "${apt_options[@]}" update
apt-get "${apt_options[@]}" --download-only --no-install-recommends -y install \
	busybox-static python3 python3-click python3-libfdt python3-pyudev python3-yaml

for package in "${archives}"/*.deb; do
	dpkg-deb -x "${package}" "${runtime}"
done

# Ubuntu uses a merged-/usr filesystem, but extracting packages without the
# base-files package does not create the top-level compatibility symlinks.
# The RISC-V dynamic loader requested by Python lives below /usr/lib.
ln -s usr/lib "${runtime}/lib"

site_dir="${runtime}/usr/lib/python3/dist-packages"
mkdir -p "${site_dir}"
cp -a "${kerf_dir}/src/kerf" "${site_dir}/kerf"
rm -rf -- "${runtime}/usr/share/doc" "${runtime}/usr/share/man" \
	"${runtime}/usr/share/locale" "${runtime}/var" "${runtime}/etc"

test -x "${runtime}/usr/bin/busybox"
test -x "${runtime}/usr/bin/python3"
test -e "${runtime}/lib/ld-linux-riscv64-lp64d.so.1"
readelf -h "${runtime}/usr/bin/python3" | grep -q 'Machine:.*RISC-V'
find "${runtime}" -type d -name __pycache__ -prune -exec rm -rf -- {} +
touch "${runtime}/.ready"
printf 'MK_KERF_RUNTIME_OK platform=riscv python=%s bytes=%s\n' \
	"$(basename "$(readlink -f "${runtime}/usr/bin/python3")")" \
	"$(du -sb "${runtime}" | awk '{print $1}')"
