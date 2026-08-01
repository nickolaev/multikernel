from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_SECONDARY = ROOT / "scripts/build-secondary-deb.sh"
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
        full_sha = "5efa61c3864013a57b17ac9fd37addf70eaf4c92"
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
            package_root = root / "package-root"
            harness_build = root / "package-test"
            subprocess.run(
                [
                    str(EXTRACT_PACKAGES),
                    str(image),
                    str(secondary),
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
            manifest = next(
                package_root.glob(
                    "usr/share/doc/linux-multikernel-secondary-*/build-manifest"
                )
            ).read_text(encoding="utf-8")
            self.assertIn(f"linux_commit={full_sha}", manifest)
            self.assertIn("track=vf-sriov-assign", manifest)
