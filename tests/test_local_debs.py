from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_SECONDARY = ROOT / "scripts/build-secondary-deb.sh"
BUILD_INITRAMFS = ROOT / "scripts/build-initramfs-deb.sh"
BUILD_META = ROOT / "scripts/build-meta-deb.sh"
BUILD_APT_REPO = ROOT / "scripts/build-apt-repo.sh"
EXTRACT_PACKAGES = ROOT / "scripts/extract-local-debs.sh"


class LocalDebTests(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil.which("dpkg-deb"):
            self.skipTest("dpkg-deb is required")

    @staticmethod
    def _build_image_package(
        root: Path, kernel_release: str, package_version: str
    ) -> Path:
        package = f"linux-image-{kernel_release}"
        staging = root / "image-package-root"
        (staging / "DEBIAN").mkdir(parents=True)
        (staging / "boot").mkdir()
        (staging / "DEBIAN/control").write_text(
            "\n".join(
                (
                    f"Package: {package}",
                    f"Version: {package_version}",
                    "Section: kernel",
                    "Priority: optional",
                    "Architecture: amd64",
                    "Maintainer: Test <test@example.invalid>",
                    "Description: test kernel package",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (staging / f"boot/vmlinuz-{kernel_release}").write_bytes(b"host-kernel")
        output = root / f"{package}_{package_version}_amd64.deb"
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(staging), str(output)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return output

    def test_builds_and_extracts_sha_named_kernel_payloads(self) -> None:
        kernel_release = "6.19.0-rc5-999-mk-vf-sriov-assign-g5efa61c386"
        package_version = "6.19.0~rc5-999.1+mk.vf.sriov.assign.g5efa61c386"
        initramfs_version = (
            f"{package_version}.h0123456789.k5104ec1f40"
            ".qa707ddb687.l46dc48719f"
        )
        full_sha = "5efa61c3864013a57b17ac9fd37addf70eaf4c92"
        kerf_sha = "5104ec1f4054382b3ec90f671d6996e304da9914"
        qemu_sha = "a707ddb687eb6598518e53ddad5bb8f50ada5f0a"
        lazy_cma_sha = "46dc48719f08ccd628c082ac436b7011c48cc9b9"
        harness_sha = "0123456789abcdef0123456789abcdef01234567"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deb_dir = root / "debs"
            deb_dir.mkdir()
            vmlinux = root / "vmlinux"
            vmlinux.write_bytes(b"secondary-kernel")
            subprocess.run(
                [
                    str(BUILD_SECONDARY),
                    str(deb_dir),
                    str(vmlinux),
                    kernel_release,
                    package_version,
                    "vf-sriov-assign",
                    full_sha,
                    "amd64",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            secondary = next(deb_dir.glob("linux-multikernel-secondary-*.deb"))
            image = self._build_image_package(root, kernel_release, package_version)
            host_initrd = root / "host-initrd.cpio.gz"
            secondary_initrd = root / "secondary-initrd.cpio.gz"
            host_initrd.write_bytes(b"host-initramfs")
            secondary_initrd.write_bytes(b"secondary-initramfs")
            subprocess.run(
                [
                    str(BUILD_INITRAMFS),
                    str(deb_dir),
                    str(host_initrd),
                    str(secondary_initrd),
                    kernel_release,
                    initramfs_version,
                    package_version,
                    "vf-sriov-assign",
                    full_sha,
                    kerf_sha,
                    qemu_sha,
                    lazy_cma_sha,
                    harness_sha,
                    "amd64",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            initramfs = next(deb_dir.glob("multikernel-initramfs-*.deb"))
            subprocess.run(
                [
                    str(BUILD_META),
                    str(deb_dir),
                    "multikernel-vf-sriov-assign",
                    initramfs_version,
                    kernel_release,
                    package_version,
                    f"multikernel-initramfs-{kernel_release}",
                    "vf-sriov-assign",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            meta = next(deb_dir.glob("multikernel-vf-sriov-assign_*.deb"))
            initramfs_depends = subprocess.run(
                ["dpkg-deb", "-f", str(initramfs), "Depends"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            self.assertEqual(
                initramfs_depends,
                f"linux-image-{kernel_release} (= {package_version}), "
                f"linux-multikernel-secondary-{kernel_release} (= {package_version})",
            )
            meta_depends = subprocess.run(
                ["dpkg-deb", "-f", str(meta), "Depends"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            self.assertIn(
                f"multikernel-initramfs-{kernel_release} (= {initramfs_version})",
                meta_depends,
            )
            package_root = root / "package-root"
            harness_build = root / "package-test"
            subprocess.run(
                [
                    str(EXTRACT_PACKAGES),
                    str(image),
                    str(secondary),
                    str(initramfs),
                    str(package_root),
                    str(harness_build),
                    kernel_release,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(
                (harness_build / "kernel/arch/x86/boot/bzImage").read_bytes(),
                b"host-kernel",
            )
            self.assertEqual(
                (harness_build / "kernel/vmlinux").read_bytes(),
                b"secondary-kernel",
            )
            self.assertEqual(
                (harness_build / "host-initrd.cpio.gz").read_bytes(),
                b"host-initramfs",
            )
            self.assertEqual(
                (harness_build / "secondary-initrd.cpio.gz").read_bytes(),
                b"secondary-initramfs",
            )
            manifest = next(
                package_root.glob(
                    "usr/share/doc/linux-multikernel-secondary-*/build-manifest"
                )
            ).read_text(encoding="utf-8")
            self.assertIn(f"linux_commit={full_sha}", manifest)
            self.assertIn("track=vf-sriov-assign", manifest)
            initramfs_manifest = next(
                package_root.glob(
                    "usr/share/doc/multikernel-initramfs-*/build-manifest"
                )
            ).read_text(encoding="utf-8")
            self.assertIn(f"kerf_commit={kerf_sha}", initramfs_manifest)
            self.assertIn(f"qemu_commit={qemu_sha}", initramfs_manifest)
            self.assertIn(f"lazy_cma_commit={lazy_cma_sha}", initramfs_manifest)
            self.assertIn(f"harness_commit={harness_sha}", initramfs_manifest)

            if shutil.which("dpkg-scanpackages"):
                repo = root / "apt-repo"
                result = subprocess.run(
                    [
                        str(BUILD_APT_REPO),
                        str(repo),
                        "resolute",
                        "amd64",
                        str(image),
                        str(secondary),
                        str(initramfs),
                        str(meta),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertIn("MK_APT_REPO_OK", result.stdout)
                packages = (
                    repo / "dists/resolute/main/binary-amd64/Packages"
                ).read_text(encoding="utf-8")
                self.assertIn("Package: multikernel-vf-sriov-assign", packages)
                release = (repo / "dists/resolute/Release").read_text(
                    encoding="utf-8"
                )
                self.assertIn("Codename: resolute", release)
                self.assertIn("SHA256:", release)
