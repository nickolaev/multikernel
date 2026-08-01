#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
repo_dir=${1:?usage: build-apt-repo.sh REPO_DIR CODENAME ARCH DEB...}
codename=${2:?missing codename}
architecture=${3:?missing architecture}
shift 3
(( $# > 0 )) || { printf 'at least one Debian package is required\n' >&2; exit 1; }
[[ "${codename}" =~ ^[a-z0-9][a-z0-9.-]+$ ]] || { printf 'invalid codename: %s\n' "${codename}" >&2; exit 1; }
[[ "${architecture}" =~ ^[a-z0-9][a-z0-9-]+$ ]] || { printf 'invalid architecture: %s\n' "${architecture}" >&2; exit 1; }
command -v dpkg-scanpackages >/dev/null || { printf 'dpkg-scanpackages is required (install dpkg-dev)\n' >&2; exit 1; }
command -v "${PYTHON:-python3}" >/dev/null || { printf 'python3 is required\n' >&2; exit 1; }

case "${repo_dir}" in
	*/apt-repo) ;;
	*) printf 'refusing unsafe APT repository path: %s\n' "${repo_dir}" >&2; exit 1 ;;
esac
pool="${repo_dir}/pool/main/m/multikernel"
binary_dir="${repo_dir}/dists/${codename}/main/binary-${architecture}"
rm -rf -- "${repo_dir}"
mkdir -p "${pool}" "${binary_dir}"

for package_path in "$@"; do
	[[ -f "${package_path}" ]] || { printf 'Debian package does not exist: %s\n' "${package_path}" >&2; exit 1; }
	package_arch=$(dpkg-deb -f "${package_path}" Architecture)
	[[ "${package_arch}" == "${architecture}" || "${package_arch}" == all ]] || {
		printf 'package architecture mismatch: %s is %s\n' "${package_path}" "${package_arch}" >&2
		exit 1
	}
	cp -p "${package_path}" "${pool}/"
done

(
	cd "${repo_dir}"
	dpkg-scanpackages --multiversion pool /dev/null >"dists/${codename}/main/binary-${architecture}/Packages"
)
gzip -n -9 -c "${binary_dir}/Packages" >"${binary_dir}/Packages.gz"
"${PYTHON:-python3}" "${root}/scripts/write-apt-release.py" \
	"${repo_dir}/dists/${codename}" "${codename}" "${architecture}"

if [[ -n "${APT_SIGNING_KEY:-}" ]]; then
	command -v gpg >/dev/null || { printf 'gpg is required to sign the APT repository\n' >&2; exit 1; }
	gpg --batch --yes --local-user "${APT_SIGNING_KEY}" --clearsign \
		--output "${repo_dir}/dists/${codename}/InRelease" \
		"${repo_dir}/dists/${codename}/Release"
	gpg --batch --yes --local-user "${APT_SIGNING_KEY}" --armor --detach-sign \
		--output "${repo_dir}/dists/${codename}/Release.gpg" \
		"${repo_dir}/dists/${codename}/Release"
	gpg --batch --yes --armor --export "${APT_SIGNING_KEY}" >"${repo_dir}/multikernel-archive-keyring.asc"
	signing_status=signed
else
	signing_status=unsigned
fi

repo_url=${APT_REPO_URL:-https://nickolaev.github.io/multikernel}
cat >"${repo_dir}/index.html" <<EOF
<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Multikernel APT repository</title>
<body><h1>Multikernel APT repository</h1>
<p>Ubuntu ${codename}, architecture ${architecture}.</p>
<pre>curl -fsSL ${repo_url}/multikernel-archive-keyring.asc | sudo gpg --dearmor -o /usr/share/keyrings/multikernel-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/multikernel-archive-keyring.gpg] ${repo_url} ${codename} main" | sudo tee /etc/apt/sources.list.d/multikernel.list
sudo apt update</pre></body></html>
EOF
touch "${repo_dir}/.nojekyll"
package_count=$(find "${pool}" -maxdepth 1 -type f -name '*.deb' | wc -l)
printf 'MK_APT_REPO_OK codename=%s architecture=%s packages=%s signing=%s root=%s\n' \
	"${codename}" "${architecture}" "${package_count}" "${signing_status}" "${repo_dir}"
