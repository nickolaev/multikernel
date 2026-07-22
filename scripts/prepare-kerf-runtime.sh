#!/usr/bin/env bash
set -euo pipefail

kerf_dir=${1:?usage: prepare-kerf-runtime.sh KERF_DIR BUILD_DIR PYTHON}
build_dir=${2:?missing build directory}
python=${3:?missing Python interpreter}
runtime="${build_dir}/kerf-runtime"
packages="${build_dir}/kerf-packages"

case "${runtime}" in
	*/build/*/kerf-runtime) ;;
	*) printf 'refusing unsafe runtime path: %s\n' "${runtime}" >&2; exit 1 ;;
esac

py_version=$("${python}" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')
stdlib=$("${python}" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')
click_dir=$("${python}" -c 'import click, pathlib; print(pathlib.Path(click.__file__).parent)')
site_dir="${runtime}/usr/lib/python3/dist-packages"

rm -rf -- "${runtime}" "${packages}"
mkdir -p "${runtime}/usr/bin" "${runtime}/usr/lib" "${site_dir}" "${packages}"
install -m 0755 "${python}" "${runtime}/usr/bin/python3"
cp -a "${stdlib}" "${runtime}/usr/lib/"
rm -rf -- "${runtime}/usr/lib/${py_version}/ensurepip" \
	"${runtime}/usr/lib/${py_version}/idlelib" \
	"${runtime}/usr/lib/${py_version}/test" \
	"${runtime}/usr/lib/${py_version}/tkinter" \
	"${runtime}/usr/lib/${py_version}/turtledemo" \
	"${runtime}/usr/lib/${py_version}/venv" \
	"${runtime}/usr/lib/${py_version}"/config-*

cp -a "${click_dir}" "${site_dir}/click"
cp -a "${kerf_dir}/src/kerf" "${site_dir}/kerf"

(
	cd "${packages}"
	apt-get download python3-libfdt
)
libfdt_package=$(find "${packages}" -maxdepth 1 -type f -name 'python3-libfdt_*.deb' -print -quit)
[[ -n "${libfdt_package}" ]] || { printf 'python3-libfdt download produced no package\n' >&2; exit 1; }
dpkg-deb -x "${libfdt_package}" "${runtime}"

declare -A libraries=()
while IFS= read -r candidate; do
	while IFS= read -r library; do
		[[ -n "${library}" ]] && libraries["${library}"]=1
	done < <(ldd "${candidate}" 2>/dev/null | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\//) { print $i; break } }')
done < <(find "${runtime}" -type f \( -perm /111 -o -name '*.so*' \) -print)

for library in "${!libraries[@]}"; do
	install -D -m 0755 "${library}" "${runtime}${library}"
done

PYTHONDONTWRITEBYTECODE=1 PYTHONHOME="${runtime}/usr" PYTHONPATH="${site_dir}" \
	"${runtime}/usr/bin/python3" -c 'import click, kerf.cli, libfdt'
find "${runtime}" -type d -name __pycache__ -prune -exec rm -rf -- {} +
touch "${runtime}/.ready"
printf 'MK_KERF_RUNTIME_OK python=%s bytes=%s\n' \
	"${py_version}" "$(du -sb "${runtime}" | awk '{print $1}')"
