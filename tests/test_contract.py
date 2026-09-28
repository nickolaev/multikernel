import unittest
from pathlib import Path

from harness.contract import (
    ContractEvidenceError,
    MODES,
    PASS_MARKERS,
    REJECTION_MARKERS,
    validate_contract_log,
)
from harness.contract_qemu import ContractConfig
from harness.qemu import HarnessError


KERNEL_SHA = "1" * 40
FIXTURE_SHA = "2" * 40


def complete_log(mode: str) -> str:
    lines = [f"MK_CONTRACT_EVIDENCE mode={mode}"]
    if mode == "boot_window":
        lines.append(
            "MK_BOOT_CONTRACT_RESULT fired=1 acked=1 status=2 send_ret=0 result=1"
        )
    else:
        lines.append(f"MK_REJECT_CONTRACT_INJECT mode={mode}")
        if mode in REJECTION_MARKERS:
            lines.extend(
                (
                    REJECTION_MARKERS[mode],
                    "Force halting multikernel instance 1 via NMI",
                )
            )
    lines.extend((PASS_MARKERS[mode], "MK_CONTRACT_HOST_PASS"))
    return "\n".join(lines) + "\n"


class ContractLogTests(unittest.TestCase):
    def test_accepts_each_complete_mode(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                validate_contract_log(complete_log(mode), mode)

    def test_rejects_missing_mode_proof(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                with self.assertRaises(ContractEvidenceError):
                    validate_contract_log("MK_CONTRACT_HOST_PASS\n", mode)

    def test_rejects_failure_marker(self) -> None:
        with self.assertRaisesRegex(ContractEvidenceError, "failure-marker"):
            validate_contract_log(
                complete_log("parent_missing") + "MK_CONTRACT_FAIL reason=test\n",
                "parent_missing",
            )

    def test_rejects_missing_or_late_child_rejection_stage(self) -> None:
        for mode in REJECTION_MARKERS:
            with self.subTest(mode=mode, case="missing"):
                text = complete_log(mode).replace(REJECTION_MARKERS[mode] + "\n", "")
                with self.assertRaisesRegex(
                    ContractEvidenceError, "missing-rejection-stage"
                ):
                    validate_contract_log(text, mode)
            with self.subTest(mode=mode, case="late"):
                text = complete_log(mode).replace(REJECTION_MARKERS[mode] + "\n", "")
                text += REJECTION_MARKERS[mode] + "\n"
                with self.assertRaisesRegex(
                    ContractEvidenceError, "invalid-rejection-order"
                ):
                    validate_contract_log(text, mode)


class ContractConfigTests(unittest.TestCase):
    def environment(self) -> dict[str, str]:
        return {
            "CONTRACT_KERNEL_SHA": KERNEL_SHA,
            "CONTRACT_FIXTURE_SHA": FIXTURE_SHA,
            "CONTRACT_LINUX_DIR": "/linux",
            "CONTRACT_FIXTURE_DIR": "/fixture",
            "QEMU_CPUS": "3",
            "QEMU_MEMORY_MB": "2048",
            "QEMU_TIMEOUT": "90",
            "QEMU_IDLE_TIMEOUT": "10",
        }

    def test_builds_mode_specific_qemu_arguments(self) -> None:
        config = ContractConfig.from_environment(
            Path("/fixture"),
            "bad_magic",
            self.environment(),
        )
        arguments = config.qemu_args()
        append = arguments[arguments.index("-append") + 1]
        self.assertIn("mk_contract_test=bad_magic", append)
        self.assertIn(f"mk_contract_kernel_sha={KERNEL_SHA}", append)

    def test_rejects_invalid_mode_or_topology(self) -> None:
        environment = self.environment()
        environment["QEMU_CPUS"] = "2"
        with self.assertRaises(HarnessError):
            ContractConfig.from_environment(
                Path("/fixture"),
                "boot_window",
                environment,
            )


class ContractPackagingTests(unittest.TestCase):
    def root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def test_kernel_config_supports_one_shot_probe_cleanup(self) -> None:
        config = (self.root() / "config/multikernel-qemu.config").read_text()
        self.assertIn("CONFIG_KPROBES=y", config)
        self.assertIn("CONFIG_MODULE_UNLOAD=y", config)

    def test_parent_rejections_use_the_early_serial_console(self) -> None:
        source = (self.root() / "harness/contract_primary.py").read_text()
        self.assertIn("earlycon=uart8250,io,0x3f8,115200n8", source)

    def test_host_initramfs_packages_both_fault_modules(self) -> None:
        script = (self.root() / "scripts/build-initramfs.sh").read_text()
        self.assertIn("mk_boot_contract_test.ko", script)
        self.assertIn("mk_reject_contract_test.ko", script)
        self.assertIn("contract_secondary.py", script)

    def test_init_scripts_select_contract_agents(self) -> None:
        host = (self.root() / "initramfs/host-init").read_text()
        child = (self.root() / "initramfs/secondary-init").read_text()
        self.assertIn("mk_contract_test=", host)
        self.assertIn("harness.contract_primary", host)
        self.assertIn("mk_contract_child=1", child)
        self.assertIn("harness.contract_secondary", child)

    def test_bad_magic_is_declared_disposable(self) -> None:
        primary = (self.root() / "harness/contract_primary.py").read_text()
        self.assertIn("disposition=unreclaimable disposable_qemu=1", primary)
        self.assertIn('_force_confirm_parked("contract-boot-window-confirm")', primary)
        self.assertIn('_force_confirm_parked(f"contract-{mode}-confirm")', primary)
