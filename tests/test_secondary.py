import tempfile
import unittest
from pathlib import Path

from harness.secondary import SecondaryConfig, parse_bar, parse_cmdline, pci_scope


class SecondaryAgentTests(unittest.TestCase):
    def test_cmdline_defaults_match_igb_parity_scenario(self):
        values = parse_cmdline("quiet mk_vf_bdf=0000:00:12.0 panic=-1")
        config = SecondaryConfig(values)

        self.assertEqual(config.instance, 1)
        self.assertEqual(config.pf_bdf, "0000:00:02.0")
        self.assertEqual(config.vf_bdf, "0000:00:12.0")
        self.assertEqual(config.vendor, "0x8086")
        self.assertEqual(config.device, "0x10ca")
        self.assertEqual(config.driver, "igbvf")
        self.assertEqual(config.bar, 0)
        self.assertEqual(config.address, "10.0.2.15/24")
        self.assertEqual(config.peer, "10.0.2.2")
        self.assertEqual(config.primary_peer, "10.0.2.14")
    def test_cmdline_can_describe_another_vf_scenario(self):
        values = parse_cmdline(
            " ".join(
                [
                    "mk_instance_id=3",
                    "mk_pf_bdf=0000:00:08.0",
                    "mk_vf_bdf=0000:00:09.0",
                    "mk_vf_vendor=0x8086",
                    "mk_vf_device=0x10ca",
                    "mk_vf_driver=igbvf",
                    "mk_vf_bar=1",
                    "mk_vf_address=10.0.3.15/24",
                    "mk_vf_peer=10.0.3.2",
                    "mk_primary_peer=10.0.3.14",
                ]
            )
        )
        config = SecondaryConfig(values)

        self.assertEqual(config.instance, 3)
        self.assertEqual(config.pf_bdf, "0000:00:08.0")
        self.assertEqual(config.vf_bdf, "0000:00:09.0")
        self.assertEqual(config.vendor, "0x8086")
        self.assertEqual(config.driver, "igbvf")
        self.assertEqual(config.bar, 1)
        self.assertEqual(config.peer, "10.0.3.2")

    def test_parses_first_pci_bar(self):
        self.assertEqual(
            parse_bar("0x00000000fea00000 0x00000000fea03fff 0x0000000000040200"),
            ("0x00000000fea00000", "0x00000000fea03fff", "0x0000000000040200"),
        )
        with self.assertRaises(ValueError):
            parse_bar("0x0 0x0")

    def test_reports_visible_and_unexpected_pci_functions(self):
        with tempfile.TemporaryDirectory() as directory:
            devices = Path(directory)
            (devices / "0000:00:12.0").mkdir()
            self.assertEqual(pci_scope(devices, "0000:00:12.0"), (1, ""))
            (devices / "0000:00:02.0").mkdir()
            self.assertEqual(
                pci_scope(devices, "0000:00:12.0"),
                (2, "0000:00:02.0"),
            )


if __name__ == "__main__":
    unittest.main()
