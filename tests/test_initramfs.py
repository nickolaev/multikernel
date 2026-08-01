from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build-initramfs.sh"


class HostInitramfsTests(unittest.TestCase):
    def test_stages_qemu_and_its_dynamic_loader_dependencies(self) -> None:
        busybox = shutil.which("busybox")
        if not busybox or not shutil.which("cpio"):
            self.skipTest("busybox and cpio are required to build an initramfs")
        qemu_binary = Path("/bin/true")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "build"
            build.mkdir()
            runtime = root / "kerf-runtime"
            runtime.mkdir()
            kernel = root / "vmlinux"
            secondary_initrd = root / "secondary-initrd.cpio.gz"
            module = root / "lazy_cma.ko"
            for artifact in (kernel, secondary_initrd, module):
                artifact.write_bytes(b"test")
            output = build / "host-initrd.cpio.gz"
            subprocess.run(
                [
                    str(SCRIPT),
                    "host",
                    str(output),
                    busybox,
                    str(ROOT / "initramfs/host-init"),
                    str(runtime),
                    str(kernel),
                    str(secondary_initrd),
                    str(module),
                    "/bin/true",
                    str(ROOT / "harness"),
                    str(qemu_binary),
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            staging = build / "initramfs-host"
            self.assertTrue(output.is_file())
            self.assertTrue((staging / "usr/bin/qemu-system-x86_64").is_file())
            libraries = {
                Path(token)
                for line in subprocess.run(
                    ["ldd", str(qemu_binary)],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.splitlines()
                for token in line.split()
                if token.startswith("/")
            }
            self.assertTrue(libraries)
            for library in libraries:
                self.assertTrue((staging / library.relative_to("/")).is_file())


if __name__ == "__main__":
    unittest.main()
