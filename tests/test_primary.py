from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from harness.primary import (
    MULTIKERNEL_QEMU,
    SECONDARY_INITRD,
    SECONDARY_KERNEL,
    ScenarioFailure,
    launch_qemu,
    qemu_arguments,
    terminate_qemu,
    wait_for_qemu_active,
)


class QemuCommandTests(unittest.TestCase):
    def test_uses_only_the_multikernel_boot_contract(self) -> None:
        self.assertEqual(
            qemu_arguments("0000:00:12.0"),
            [
                MULTIKERNEL_QEMU,
                "-machine",
                "multikernel",
                "-accel",
                "multikernel,instance-id=1",
                "-kernel",
                SECONDARY_KERNEL,
                "-initrd",
                SECONDARY_INITRD,
                "-append",
                "console=mktty0 rdinit=/init quiet loglevel=6 panic=-1 "
                "mk_vf_bdf=0000:00:12.0",
                "-nographic",
                "-monitor",
                "none",
            ],
        )

    @patch("harness.primary.subprocess.Popen")
    def test_launch_captures_qemu_console(self, popen: MagicMock) -> None:
        process = MagicMock()
        process.stdout = MagicMock()
        popen.return_value = process

        self.assertIs(launch_qemu("0000:00:12.0"), process)
        popen.assert_called_once_with(
            qemu_arguments("0000:00:12.0"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    @patch("harness.primary.subprocess.Popen", side_effect=OSError("missing"))
    def test_launch_reports_process_errors(self, _popen: MagicMock) -> None:
        with self.assertRaisesRegex(ScenarioFailure, "qemu-launch"):
            launch_qemu("0000:00:12.0")

    @patch("harness.primary.subprocess.Popen")
    def test_launch_rejects_missing_console(self, popen: MagicMock) -> None:
        process = MagicMock()
        process.stdout = None
        popen.return_value = process

        with self.assertRaisesRegex(ScenarioFailure, "qemu-stdout"):
            launch_qemu("0000:00:12.0")


class QemuStartupTests(unittest.TestCase):
    @patch("harness.primary.emit")
    @patch("harness.primary.read_text", return_value="active")
    def test_reports_active_status(
        self, _read_text: MagicMock, emit: MagicMock
    ) -> None:
        process = MagicMock()

        wait_for_qemu_active(process, MagicMock(), attempts=1)

        emit.assert_called_once_with(
            "MK_STAGE_STATUS_active name=qemu-demo id=1"
        )
        process.poll.assert_not_called()

    @patch("harness.primary.emit")
    @patch("harness.primary.read_text", return_value="ready")
    def test_preserves_early_qemu_error(
        self, _read_text: MagicMock, emit: MagicMock
    ) -> None:
        process = MagicMock()
        process.poll.return_value = 2
        process.stdout.read.return_value = b"qemu: boot failed\n"

        with self.assertRaisesRegex(ScenarioFailure, "qemu-exit-2"):
            wait_for_qemu_active(process, MagicMock(), attempts=1)

        emit.assert_called_once_with("MK_QEMU_OUTPUT qemu: boot failed")

    @patch("harness.primary.time.sleep")
    @patch("harness.primary.emit")
    @patch("harness.primary.read_text", return_value="ready")
    def test_bounds_startup_wait(
        self, _read_text: MagicMock, emit: MagicMock, _sleep: MagicMock
    ) -> None:
        process = MagicMock()
        process.poll.return_value = None

        with self.assertRaisesRegex(ScenarioFailure, "status-qemu-demo-active"):
            wait_for_qemu_active(process, MagicMock(), attempts=1)

        self.assertIn("expected=active actual=ready", emit.call_args.args[0])


class QemuTerminationTests(unittest.TestCase):
    def test_terminates_running_qemu(self) -> None:
        process = MagicMock()
        process.poll.return_value = None

        terminate_qemu(process)

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=30)
        process.kill.assert_not_called()

    def test_kills_and_reports_a_qemu_termination_timeout(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("qemu", 30), None]

        with self.assertRaisesRegex(ScenarioFailure, "qemu-terminate"):
            terminate_qemu(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list[1].kwargs, {"timeout": 5})

    def test_leaves_an_already_exited_qemu_alone(self) -> None:
        process = MagicMock()
        process.poll.return_value = 0

        terminate_qemu(process)

        process.terminate.assert_not_called()
        process.wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
