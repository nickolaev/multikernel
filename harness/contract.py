"""Validation rules for the isolated boot-contract QEMU modes."""

from __future__ import annotations


MODES = ("boot_window", "bad_magic", "parent_mismatch", "parent_missing")

FAILURE_MARKERS = (
    "MK_CONTRACT_FAIL",
    "MK_DEMO_FAIL",
    "MK_SECONDARY_FAIL",
    "BUG: sleeping function called from invalid context",
    "BUG: scheduling while atomic",
    "[ BUG: Invalid wait context ]",
    "WARNING: possible circular locking dependency detected",
)

PASS_MARKERS = {
    "boot_window": (
        "MK_CONTRACT_BOOT_WINDOW_PASS delivery=acknowledged shutdown=observed "
        "first_park=confirmed reuse=ready second_park=confirmed"
    ),
    "bad_magic": (
        "MK_CONTRACT_BAD_MAGIC_PASS rejection=boot-context "
        "disposition=unreclaimable disposable_qemu=1"
    ),
    "parent_mismatch": (
        "MK_CONTRACT_PARENT_MISMATCH_PASS "
        "rejection=invalid-parent-identity first_park=confirmed "
        "reuse=ready second_park=confirmed"
    ),
    "parent_missing": (
        "MK_CONTRACT_PARENT_MISSING_PASS rejection=no-parent-cpu "
        "first_park=confirmed reuse=ready second_park=confirmed"
    ),
}

REJECTION_MARKERS = {
    "parent_mismatch": "Invalid parent/child IPI identity:",
    "parent_missing": "No parent IPI CPU in the boot tree",
}


class ContractEvidenceError(ValueError):
    """The QEMU transcript does not prove the selected contract."""


def validate_contract_log(text: str, mode: str) -> None:
    if mode not in MODES:
        raise ContractEvidenceError(f"unknown-mode mode={mode}")
    for marker in FAILURE_MARKERS:
        if marker in text:
            raise ContractEvidenceError(f"failure-marker marker={marker!r}")
    evidence = f"MK_CONTRACT_EVIDENCE mode={mode}"
    if evidence not in text:
        raise ContractEvidenceError(f"missing-evidence mode={mode}")
    if "MK_REJECT_CONTRACT_INJECT" not in text and mode != "boot_window":
        raise ContractEvidenceError(f"missing-rejection-injection mode={mode}")
    if mode == "boot_window" and "MK_BOOT_CONTRACT_RESULT fired=1 acked=1 status=2" not in text:
        raise ContractEvidenceError("missing-boot-window-injection")
    if mode in REJECTION_MARKERS:
        injection_index = text.find("MK_REJECT_CONTRACT_INJECT")
        rejection_index = text.find(REJECTION_MARKERS[mode])
        force_index = text.find("Force halting multikernel instance")
        if rejection_index < 0:
            raise ContractEvidenceError(f"missing-rejection-stage mode={mode}")
        if force_index < 0:
            raise ContractEvidenceError(f"missing-force-confirm mode={mode}")
        if not injection_index < rejection_index < force_index:
            raise ContractEvidenceError(f"invalid-rejection-order mode={mode}")
    if PASS_MARKERS[mode] not in text:
        raise ContractEvidenceError(f"missing-pass mode={mode}")
    if "MK_CONTRACT_HOST_PASS" not in text:
        raise ContractEvidenceError(f"missing-host-pass mode={mode}")
