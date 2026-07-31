from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.baseline import render_single_vf_baseline
from harness.inventory import PciFunction


class PciInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.devices = self.root / "devices"
        self.drivers = self.root / "drivers"
        self.groups = self.root / "groups"
        self.devices.mkdir()
        self.drivers.mkdir()
        self.groups.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_function(
        self, bdf: str, vendor: str, device: str, driver: str, group: str
    ) -> PciFunction:
        path = self.devices / bdf
        path.mkdir()
        (path / "vendor").write_text(f"0x{vendor}\n")
        (path / "device").write_text(f"0x{device}\n")
        driver_path = self.drivers / driver
        driver_path.mkdir(exist_ok=True)
        (path / "driver").symlink_to(driver_path)
        group_path = self.groups / group
        (group_path / "devices").mkdir(parents=True, exist_ok=True)
        (group_path / "devices" / bdf).symlink_to(path)
        (path / "iommu_group").symlink_to(group_path)
        return PciFunction.from_path(path)

    def test_reads_identity_driver_and_singleton_group(self) -> None:
        function = self.make_function(
            "0000:00:12.0", "8086", "10ca", "igbvf", "4"
        )

        self.assertEqual(function.bdf, "0000:00:12.0")
        self.assertEqual(function.vendor, 0x8086)
        self.assertEqual(function.device, 0x10CA)
        self.assertEqual(function.driver, "igbvf")
        self.assertEqual(function.iommu_group, "4")
        self.assertEqual(function.iommu_group_members(), ("0000:00:12.0",))

    def test_resolves_virtual_function_from_pf_link(self) -> None:
        pf = self.make_function("0000:00:02.0", "8086", "10c9", "igb", "2")
        vf = self.make_function(
            "0000:00:12.0", "8086", "10ca", "igbvf", "4"
        )
        (pf.path / "virtfn0").symlink_to(vf.path)

        self.assertEqual(pf.virtual_function(0), vf)
        self.assertIsNone(pf.virtual_function(1))

    def test_renders_owned_baseline_from_inventory(self) -> None:
        pf = self.make_function("0000:00:02.0", "8086", "10c9", "igb", "2")
        vf = self.make_function(
            "0000:00:12.0", "8086", "10ca", "igbvf", "4"
        )

        dts = render_single_vf_baseline("0x1d9000000", pf, vf)

        self.assertIn("memory-base = <0x1d9000000>;", dts)
        self.assertIn('pci-id = "0000:00:02.0";', dts)
        self.assertIn('pci-id = "0000:00:12.0";', dts)
        self.assertIn("vendor-id = <0x8086>;", dts)
        self.assertIn("device-id = <0x10ca>;", dts)
        self.assertIn("ecam-base = /bits/ 64 <0xb0000000>;", dts)


if __name__ == "__main__":
    unittest.main()
