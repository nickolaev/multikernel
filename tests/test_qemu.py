from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from harness.events import encode_event
from harness.qemu import (
    FAILURE_MARKERS,
    FORBIDDEN_RELIABILITY_COUNTERS,
    REQUIRED_EVENT_NAMES,
    REQUIRED_MARKERS,
    HarnessConfig,
    HarnessError,
    QmpClient,
    ProgressWatchdog,
    validate_log,
)


class HarnessConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/workspace/multikernel")

    def test_default_qemu_topology_matches_complex_sriov_scenario(self) -> None:
        config = HarnessConfig.from_environment(self.root, {})

        self.assertEqual(config.cpus, 12)
        self.assertEqual(config.memory_mb, 8192)
        self.assertEqual(config.timeout_seconds, 1200)
        self.assertEqual(config.idle_timeout_seconds, 120)
        self.assertEqual(config.build_dir, self.root / "build")
        self.assertIn("q35,accel=tcg", config.qemu_args())
        self.assertIn("intel-iommu,intremap=on", config.qemu_args())
        self.assertIn("igb,id=igb0,addr=0x2,netdev=igb0-net", config.qemu_args())
        self.assertIn(
            "igb,id=igb1,bus=igb1-port,addr=0x0,netdev=igb1-net",
            config.qemu_args(),
        )
        self.assertIn(
            f"unix:{config.qmp_socket},server=on,wait=off", config.qemu_args()
        )

    def test_environment_overrides_paths_and_numeric_settings(self) -> None:
        config = HarnessConfig.from_environment(
            self.root,
            {
                "BUILD_DIR": "/tmp/mk-build",
                "QEMU": "/usr/bin/qemu-system-x86_64",
                "QEMU_CPUS": "12",
                "QEMU_MEMORY_MB": "8192",
                "QEMU_TIMEOUT": "600",
                "QEMU_IDLE_TIMEOUT": "30",
            },
        )

        self.assertEqual(config.build_dir, Path("/tmp/mk-build"))
        self.assertEqual(config.qemu, "/usr/bin/qemu-system-x86_64")
        self.assertEqual(config.cpus, 12)
        self.assertEqual(config.memory_mb, 8192)
        self.assertEqual(config.timeout_seconds, 600)
        self.assertEqual(config.idle_timeout_seconds, 30)

    def test_rejects_non_numeric_tunable(self) -> None:
        with self.assertRaisesRegex(HarnessError, "must be numeric"):
            HarnessConfig.from_environment(self.root, {"QEMU_CPUS": "four"})

    def test_rejects_insufficient_resources(self) -> None:
        with self.assertRaisesRegex(HarnessError, "exactly 12"):
            HarnessConfig.from_environment(self.root, {"QEMU_CPUS": "4"})
        with self.assertRaisesRegex(HarnessError, "exactly 8192"):
            HarnessConfig.from_environment(
                self.root, {"QEMU_MEMORY_MB": "6144"}
            )


class LogValidationTests(unittest.TestCase):
    @staticmethod
    def complete_log() -> str:
        event_lines = [
            encode_event(
                event,
                ({"max_cpus": 9} if event == "MK_CONCURRENT_CPU_PCI_RPC_PASS" else
                 ({"active_instances": 3, "leases": 3}
                  if event == "MK_COMPLEX_CONCURRENT_LEASES_PASS" else
                  ({"cycles": 100} if event == "MK_RESPAWN_STRESS_PASS" else
                   ({name: 0 for name in FORBIDDEN_RELIABILITY_COUNTERS} |
                    {"tx_before": 0, "tx_after": 1, "rx_before": 0, "rx_after": 1}
                    if event == "MK_SECONDARY_VF_DATAPATH" else {})))),
                "primary",
            )
            for event in REQUIRED_EVENT_NAMES
        ]
        topology_lines = [
            "setup_percpu: NR_CPUS:12",
            "MK_STAGE_KERF_INIT_OK cpus=2,3,4,5,6,7,8,9,10,11 memory=1024M",
        ]
        return "\n".join([*topology_lines, *REQUIRED_MARKERS, *event_lines])

    def test_accepts_complete_log(self) -> None:
        validate_log(self.complete_log())

    def test_rejects_guest_failure_markers(self) -> None:
        for marker in FAILURE_MARKERS:
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(HarnessError, "guest-failure-marker"):
                    validate_log(f"{marker} reason=test")

    def test_reports_missing_marker(self) -> None:
        present = "\n".join(
            [
                *REQUIRED_MARKERS[:-1],
                *[encode_event(event, {}, "primary") for event in REQUIRED_EVENT_NAMES],
            ]
        )
        with self.assertRaisesRegex(
            HarnessError, "missing-marker.*MK_DEMO_PASS"
        ):
            validate_log(present)

    def test_reports_missing_structured_event(self) -> None:
        present = "\n".join(
            [
                *REQUIRED_MARKERS,
                *[
                    encode_event(event, {}, "primary")
                    for event in REQUIRED_EVENT_NAMES[:-1]
                ],
            ]
        )
        with self.assertRaisesRegex(HarnessError, "missing-event.*MK_DEMO_PASS"):
            validate_log(present)

    def test_rejects_duplicate_structured_event(self) -> None:
        event = encode_event("MK_DEMO_PASS", {}, "primary")
        with self.assertRaisesRegex(HarnessError, "duplicate-event.*MK_DEMO_PASS"):
            validate_log(self.complete_log() + "\n" + event)

    def test_requires_datapath_reliability_counters(self) -> None:
        event = encode_event(
            "MK_SECONDARY_VF_DATAPATH",
            {"tx_before": 0, "tx_after": 1, "rx_before": 0, "rx_after": 1},
            "secondary",
        )
        with self.assertRaisesRegex(HarnessError, "malformed-reliability-counters"):
            validate_log(
                self.complete_log().replace(
                    next(
                        line
                        for line in self.complete_log().splitlines()
                        if '"event":"MK_SECONDARY_VF_DATAPATH"' in line
                    ),
                    event,
                )
            )

    def test_requires_nr_cpus_12_boot_evidence(self) -> None:
        present = self.complete_log().replace("setup_percpu: NR_CPUS:12", "")

        with self.assertRaisesRegex(HarnessError, "missing-topology-evidence"):
            validate_log(present)

    def test_requires_full_multikernel_cpu_pool_evidence(self) -> None:
        present = self.complete_log().replace(
            "MK_STAGE_KERF_INIT_OK cpus=2,3,4,5,6,7,8,9,10,11 memory=1024M",
            "MK_STAGE_KERF_INIT_OK cpus=2,3,4,5 memory=1024M",
        )

        with self.assertRaisesRegex(HarnessError, "missing-topology-evidence"):
            validate_log(present)

    def test_requires_concurrent_cpu_update_to_exceed_initial_cpuset_capacity(self) -> None:
        present = self.complete_log().replace(
            encode_event(
                "MK_CONCURRENT_CPU_PCI_RPC_PASS", {"max_cpus": 9}, "primary"
            ),
            encode_event(
                "MK_CONCURRENT_CPU_PCI_RPC_PASS", {"max_cpus": 8}, "primary"
            ),
        )

        with self.assertRaisesRegex(HarnessError, "insufficient-cpuset-growth"):
            validate_log(present)

    def test_rejects_forbidden_counter_values(self) -> None:
        event = encode_event(
            "MK_SECONDARY_VF_DATAPATH",
            {"tx_before": 4, "tx_after": 3, "rx_before": 1, "rx_after": 2},
            "secondary",
        )
        complete = self.complete_log()
        old = next(
            line for line in complete.splitlines()
            if '"event":"MK_SECONDARY_VF_DATAPATH"' in line
        )
        with self.assertRaisesRegex(HarnessError, "forbidden-counter-value"):
            validate_log(complete.replace(old, event))

    def test_rejects_one_child_lease_claim(self) -> None:
        complete = self.complete_log().replace(
            '"active_instances":3', '"active_instances":1'
        )
        with self.assertRaisesRegex(HarnessError, "missing-multi-child-proof"):
            validate_log(complete)

    def test_rejects_short_respawn_claim(self) -> None:
        complete = self.complete_log().replace(
            '"cycles":100', '"cycles":99'
        )
        with self.assertRaisesRegex(HarnessError, "missing-respawn-proof"):
            validate_log(complete)


class ProgressWatchdogTests(unittest.TestCase):
    def test_structured_event_resets_watchdog_but_console_chatter_does_not(self) -> None:
        now = [0.0]
        watchdog = ProgressWatchdog(120, clock=lambda: now[0])
        watchdog.feed("kernel chatter\n")
        now[0] = 119.0
        self.assertFalse(watchdog.stalled())
        watchdog.feed(encode_event("progress", {}, "primary") + "\n")
        now[0] = 238.0
        self.assertFalse(watchdog.stalled())
        now[0] = 358.0
        self.assertTrue(watchdog.stalled())

    def test_invalid_structured_record_does_not_reset_watchdog(self) -> None:
        now = [0.0]
        watchdog = ProgressWatchdog(10, clock=lambda: now[0])
        watchdog.feed('MK_EVENT {"event":"progress"} trailing\n')
        now[0] = 11.0
        self.assertTrue(watchdog.stalled())

    def test_handles_split_structured_event(self) -> None:
        now = [0.0]
        watchdog = ProgressWatchdog(10, clock=lambda: now[0])
        watchdog.feed("MK_EV")
        now[0] = 11.0
        self.assertTrue(watchdog.stalled())
        watchdog.feed(
            'ENT {"event":"progress","fields":{},"source":"primary"}\n'
        )
        self.assertFalse(watchdog.stalled())


class QmpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_negotiates_capabilities_and_queries_status(self) -> None:
        completed = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writer.write(b'{"QMP":{"version":{}}}\r\n')
            await writer.drain()
            capabilities = json.loads(await reader.readline())
            self.assertEqual(capabilities, {"execute": "qmp_capabilities"})
            writer.write(b'{"return":{}}\r\n')
            await writer.drain()
            query = json.loads(await reader.readline())
            self.assertEqual(query, {"execute": "query-status"})
            writer.write(b'{"return":{"status":"running"}}\r\n')
            await writer.drain()
            completed.set()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "qmp.sock"
            server = await asyncio.start_unix_server(handle, path=socket_path)
            async with server:
                client = await QmpClient.connect(socket_path)
                status = await client.execute("query-status")
                self.assertEqual(status, {"status": "running"})
                await completed.wait()
                await client.close()


if __name__ == "__main__":
    unittest.main()
