from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from harness.events import encode_event
from harness.qemu import (
    FAILURE_MARKERS,
    REQUIRED_EVENT_NAMES,
    REQUIRED_MARKERS,
    HarnessConfig,
    HarnessError,
    QmpClient,
    validate_log,
)


class HarnessConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/workspace/multikernel")

    def test_default_qemu_topology_matches_complex_sriov_scenario(self) -> None:
        config = HarnessConfig.from_environment(self.root, {})

        self.assertEqual(config.cpus, 12)
        self.assertEqual(config.memory_mb, 8192)
        self.assertEqual(config.timeout_seconds, 600)
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
        with self.assertRaisesRegex(HarnessError, "at least 5"):
            HarnessConfig.from_environment(self.root, {"QEMU_CPUS": "4"})
        with self.assertRaisesRegex(HarnessError, "at least 7168"):
            HarnessConfig.from_environment(
                self.root, {"QEMU_MEMORY_MB": "6144"}
            )


class LogValidationTests(unittest.TestCase):
    @staticmethod
    def complete_log() -> str:
        event_lines = [
            encode_event(
                event,
                {"max_cpus": 9} if event == "MK_CONCURRENT_CPU_PCI_RPC_PASS" else {},
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
