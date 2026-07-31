from __future__ import annotations

import unittest
from pathlib import Path

from harness.qemu import (
    FAILURE_MARKERS,
    REQUIRED_MARKERS,
    HarnessConfig,
    HarnessError,
    validate_log,
)


class HarnessConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/workspace/multikernel")

    def test_default_qemu_topology_matches_single_igb_scenario(self) -> None:
        config = HarnessConfig.from_environment(self.root, {})

        self.assertEqual(config.cpus, 4)
        self.assertEqual(config.memory_mb, 6144)
        self.assertEqual(config.timeout_seconds, 300)
        self.assertEqual(config.build_dir, self.root / "build")
        self.assertIn("q35,accel=tcg", config.qemu_args())
        self.assertIn("intel-iommu,intremap=on", config.qemu_args())
        self.assertIn("igb,netdev=net0", config.qemu_args())

    def test_environment_overrides_paths_and_numeric_settings(self) -> None:
        config = HarnessConfig.from_environment(
            self.root,
            {
                "BUILD_DIR": "/tmp/mk-build",
                "QEMU": "/usr/bin/qemu-system-x86_64",
                "QEMU_CPUS": "8",
                "QEMU_MEMORY_MB": "8192",
                "QEMU_TIMEOUT": "600",
            },
        )

        self.assertEqual(config.build_dir, Path("/tmp/mk-build"))
        self.assertEqual(config.qemu, "/usr/bin/qemu-system-x86_64")
        self.assertEqual(config.cpus, 8)
        self.assertEqual(config.memory_mb, 8192)
        self.assertEqual(config.timeout_seconds, 600)

    def test_rejects_non_numeric_tunable(self) -> None:
        with self.assertRaisesRegex(HarnessError, "must be numeric"):
            HarnessConfig.from_environment(self.root, {"QEMU_CPUS": "four"})

    def test_rejects_insufficient_resources(self) -> None:
        with self.assertRaisesRegex(HarnessError, "at least 3"):
            HarnessConfig.from_environment(self.root, {"QEMU_CPUS": "2"})
        with self.assertRaisesRegex(HarnessError, "at least 5120"):
            HarnessConfig.from_environment(
                self.root, {"QEMU_MEMORY_MB": "4096"}
            )


class LogValidationTests(unittest.TestCase):
    def test_accepts_complete_log(self) -> None:
        validate_log("\n".join(REQUIRED_MARKERS))

    def test_rejects_guest_failure_markers(self) -> None:
        for marker in FAILURE_MARKERS:
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(HarnessError, "guest-failure-marker"):
                    validate_log(f"{marker} reason=test")

    def test_reports_missing_marker(self) -> None:
        present = "\n".join(REQUIRED_MARKERS[:-1])
        with self.assertRaisesRegex(
            HarnessError, "missing-marker.*MK_DEMO_PASS"
        ):
            validate_log(present)


if __name__ == "__main__":
    unittest.main()
