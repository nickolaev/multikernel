import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from harness.secondary import (
    SecondaryConfig,
    SecondaryScenario,
    parse_bar,
    parse_cmdline,
    pci_identity_matches,
    pci_scope,
)


class RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


class SecondaryAgentTests(unittest.TestCase):
    @mock.patch("harness.secondary.subprocess.run")
    def test_busybox_timeout_is_a_failed_command(self, run):
        run.side_effect = subprocess.TimeoutExpired(["/bin/busybox", "ping"], 15)
        self.assertFalse(SecondaryScenario.run_busybox("ping"))

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

    def test_matches_little_endian_pci_identity(self):
        self.assertTrue(
            pci_identity_matches(bytes.fromhex("8680ca10"), "0x8086", "0x10ca")
        )
        self.assertFalse(
            pci_identity_matches(bytes.fromhex("8680cb10"), "0x8086", "0x10ca")
        )
        self.assertFalse(pci_identity_matches(b"\x86\x80", "0x8086", "0x10ca"))

    def test_verify_datapath_returns_success(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = Path(directory)
            net_path = sysfs / "class/net/eth0"
            statistics = net_path / "statistics"
            statistics.mkdir(parents=True)
            (net_path / "address").write_text("02:00:00:00:00:01\n")
            (net_path / "operstate").write_text("up\n")
            tx_packets = statistics / "tx_packets"
            rx_packets = statistics / "rx_packets"
            tx_packets.write_text("0\n")
            rx_packets.write_text("0\n")
            config = SecondaryConfig(
                parse_cmdline(
                    "mk_vf_bdf=0000:00:12.0 mk_primary_peer=none"
                )
            )
            sink = RecordingSink()
            scenario = SecondaryScenario(config, sink, sysfs)
            busybox_calls = []

            def run_busybox(*arguments):
                busybox_calls.append(arguments)
                if arguments[0] == "ping":
                    tx_packets.write_text("1\n")
                    rx_packets.write_text("1\n")
                return True

            scenario.run_busybox = run_busybox

            self.assertTrue(scenario.verify_datapath("eth0"))
            self.assertIn(
                ("ping", "-I", "eth0"),
                [call[:3] for call in busybox_calls],
            )
            self.assertIn(
                "MK_SECONDARY_VF_DATAPATH",
                [event for event, _fields in sink.events],
            )

    def test_wait_for_carrier_observes_sysfs_state(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = Path(directory)
            net_path = sysfs / "class/net/eth0"
            net_path.mkdir(parents=True)
            (net_path / "carrier").write_text("1\n")
            config = SecondaryConfig(parse_cmdline("mk_vf_bdf=0000:00:12.0"))
            scenario = SecondaryScenario(config, RecordingSink(), sysfs)

            self.assertTrue(scenario.wait_for_carrier("eth0"))

    def test_verify_datapath_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = Path(directory)
            net_path = sysfs / "class/net/eth0"
            statistics = net_path / "statistics"
            statistics.mkdir(parents=True)
            (net_path / "address").write_text("02:00:00:00:00:01\n")
            (net_path / "operstate").write_text("up\n")
            (statistics / "tx_packets").write_text("0\n")
            (statistics / "rx_packets").write_text("0\n")
            config = SecondaryConfig(
                parse_cmdline(
                    "mk_vf_bdf=0000:00:12.0 mk_primary_peer=none"
                )
            )
            sink = RecordingSink()
            scenario = SecondaryScenario(config, sink, sysfs)
            scenario.run_busybox = lambda *_arguments: False

            self.assertFalse(scenario.verify_datapath("eth0"))

    def test_reset_and_rebind_pass_require_datapath_success(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = Path(directory)
            driver_path = sysfs / "bus/pci/drivers/igbvf"
            driver_path.mkdir(parents=True)
            (driver_path / "unbind").write_text("")
            (driver_path / "bind").write_text("")
            vf_path = sysfs / "bus/pci/devices/0000:00:12.0"
            vf_path.mkdir(parents=True)
            (vf_path / "reset").write_text("")
            config = SecondaryConfig(parse_cmdline("mk_vf_bdf=0000:00:12.0"))
            sink = RecordingSink()
            scenario = SecondaryScenario(config, sink, sysfs)
            scenario.driver_name = lambda _path: ""
            scenario.configure_vf = lambda _path: "eth0"
            scenario.verify_datapath = lambda _netdev: False

            self.assertEqual(scenario.rebind_vf(vf_path), "")
            scenario.reset_vf(vf_path)

            event_names = [event for event, _fields in sink.events]
            self.assertNotIn("MK_SECONDARY_VF_REBIND_PASS", event_names)
            self.assertNotIn("MK_SECONDARY_VF_FLR_PASS", event_names)


if __name__ == "__main__":
    unittest.main()
