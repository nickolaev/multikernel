import unittest

from harness.topology import PCI_FAMILIES, complex_pci_args


class ComplexTopologyTests(unittest.TestCase):
    def test_declares_three_independent_pf_families(self):
        self.assertEqual([family.vf_count for family in PCI_FAMILIES], [4, 2, 2])
        self.assertEqual(
            [family.pf_bdf for family in PCI_FAMILIES],
            ["0000:00:02.0", "0000:01:00.0", "0000:02:00.0"],
        )
        self.assertEqual(
            [family.name for family in PCI_FAMILIES],
            ["igb0", "igb1", "igb2"],
        )

    def test_keeps_pf_buses_separate_and_adds_pci_noise(self):
        devices = "\n".join(complex_pci_args())
        self.assertIn("igb,id=igb0,addr=0x2", devices)
        self.assertIn("igb,id=igb1,bus=igb1-port", devices)
        self.assertIn("igb,id=igb2,bus=igb2-port", devices)
        self.assertNotIn("sriov-pf=", devices)
        self.assertIn("virtio-rng-pci,id=noise-rng", devices)
        self.assertIn("virtio-balloon-pci,id=noise-balloon", devices)


if __name__ == "__main__":
    unittest.main()
