"""Host-side boot-contract and rejection validation scenarios."""

from __future__ import annotations

import os
from pathlib import Path
import select
import time

from harness.baseline import render_baseline
from harness.contract import MODES
from harness.primary import BUSYBOX, INSTANCES, ScenarioFailure, command, dmesg, emit, kerf


NAME = "contract-child"
INSTANCE = 1
CPU = 2


def _mode() -> str:
    for argument in Path("/proc/cmdline").read_text().split():
        key, separator, value = argument.partition("=")
        if key == "mk_contract_test" and separator and value in MODES:
            return value
    raise ScenarioFailure("contract-mode")


def _expect_status(expected: str, timeout: float = 90) -> None:
    path = INSTANCES / NAME / "status"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if path.read_text().strip() == expected:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise ScenarioFailure(f"contract-status-{expected}")


def _wait_parameter(module: str, name: str, expected: int, timeout: float = 30) -> None:
    path = Path("/sys/module") / module / "parameters" / name
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if int(path.read_text().strip()) == expected:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise ScenarioFailure(f"contract-parameter-{module}-{name}-{expected}")


def _open_console():
    console = open("/dev/mktty", "r+b", buffering=0)
    console.write(f"{INSTANCE}\n".encode("ascii"))
    return console


def _wait_console(console, marker: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    buffer = b""
    while time.monotonic() < deadline:
        readable, _, _ = select.select([console.fileno()], [], [], 0.2)
        if not readable:
            continue
        chunk = os.read(console.fileno(), 4096)
        if not chunk:
            continue
        os.write(1, b"MK_CONTRACT_CHILD_STREAM:" + chunk)
        buffer += chunk
        if marker.encode("ascii") in buffer:
            return
        if len(buffer) > 131072:
            buffer = buffer[-65536:]
    raise ScenarioFailure(f"contract-console-{marker}")


def _allocate_pool() -> str:
    command([BUSYBOX, "insmod", "/lib/modules/lazy_cma.ko"], "contract-lazy-cma")
    result = command(
        ["/bin/lazy_cma_tool", "-a", "1024", "-n", "Multikernel Memory Pool"],
        "contract-pool-allocation",
        capture=True,
    )
    base = result.stdout.split()[-1]
    if not base.startswith("0x"):
        raise ScenarioFailure("contract-pool-address")
    return base


def _setup_instance(mode: str) -> None:
    pool_base = _allocate_pool()
    Path("/run/contract-baseline.dts").write_text(
        render_baseline(pool_base, cpus=(CPU,), memory_bytes=0x40000000, resources=())
    )
    kerf("init", "--input=/run/contract-baseline.dts", stage="contract-init")
    early_console = (
        " earlycon=uart8250,io,0x3f8,115200n8"
        if mode in ("parent_mismatch", "parent_missing")
        else ""
    )
    kerf(
        "create",
        NAME,
        f"--id={INSTANCE}",
        f"--cpus={CPU}",
        "--memory=256MB",
        stage="contract-create",
    )
    _expect_status("ready")
    kerf(
        "load",
        NAME,
        "--kernel=/payload/vmlinux",
        "--initrd=/payload/secondary-initrd.cpio.gz",
        f"--cmdline=rdinit=/init quiet loglevel=6 panic=-1 "
        f"mk_contract_child=1 mk_instance_id={INSTANCE}{early_console}",
        "--console=mktty0",
        stage="contract-load",
    )
    _expect_status("loaded")


def _force_confirm_parked(stage: str) -> None:
    before = dmesg()
    kerf("kill", "--force", NAME, stage=stage)
    _expect_status("loaded")
    after = dmesg()
    delta = after[len(before) :] if after.startswith(before) else after
    if f"Instance {INSTANCE} ({NAME}) halted, CPUs parking in pool" not in delta:
        raise ScenarioFailure(f"{stage}-park-evidence")


def _prove_reuse(console) -> None:
    kerf("exec", NAME, stage="contract-reuse-exec")
    _expect_status("active")
    _wait_console(console, f"MK_CONTRACT_CHILD_READY instance={INSTANCE}")
    kerf("kill", NAME, stage="contract-reuse-kill")
    _expect_status("loaded")


def _load_fault_module(mode: str) -> str:
    if mode == "boot_window":
        command(
            [
                BUSYBOX,
                "insmod",
                "/lib/modules/mk_boot_contract_test.ko",
                f"target_id={INSTANCE}",
                "helper_cpu=1",
            ],
            "contract-boot-module",
        )
        return "mk_boot_contract_test"
    command(
        [
            BUSYBOX,
            "insmod",
            "/lib/modules/mk_reject_contract_test.ko",
            f"mode={mode}",
        ],
        "contract-reject-module",
    )
    return "mk_reject_contract_test"


def _unload_fault_module(module: str) -> None:
    command([BUSYBOX, "rmmod", module], f"contract-unload-module-{module}")


def run() -> None:
    mode = _mode()
    values = {
        argument.partition("=")[0]: argument.partition("=")[2]
        for argument in Path("/proc/cmdline").read_text().split()
        if "=" in argument
    }
    emit(
        f"MK_CONTRACT_EVIDENCE mode={mode} "
        f"kernel_sha={values.get('mk_contract_kernel_sha', '')} "
        f"fixture_sha={values.get('mk_contract_fixture_sha', '')}"
    )
    _setup_instance(mode)
    console = _open_console()
    try:
        module = _load_fault_module(mode)
        kerf("exec", NAME, stage=f"contract-{mode}-exec")
        _expect_status("active")
        _wait_parameter(module, "fired", 1)
        _wait_parameter(module, "status", 2)

        if mode == "boot_window":
            _wait_parameter(module, "send_ret", 0)
            _wait_parameter(module, "result", 1)
            _wait_parameter(module, "acked", 1, timeout=120)
        _unload_fault_module(module)

        if mode == "bad_magic":
            emit(
                "MK_CONTRACT_BAD_MAGIC_PASS rejection=boot-context "
                "disposition=unreclaimable disposable_qemu=1"
            )
            return

        if mode == "boot_window":
            _force_confirm_parked("contract-boot-window-confirm")
            _prove_reuse(console)
            emit(
                "MK_CONTRACT_BOOT_WINDOW_PASS delivery=acknowledged shutdown=observed "
                "first_park=confirmed reuse=ready second_park=confirmed"
            )
        else:
            _force_confirm_parked(f"contract-{mode}-confirm")
            _prove_reuse(console)
            rejection = (
                "invalid-parent-identity"
                if mode == "parent_mismatch"
                else "no-parent-cpu"
            )
            emit(
                f"MK_CONTRACT_{mode.upper()}_PASS rejection={rejection} "
                "first_park=confirmed reuse=ready second_park=confirmed"
            )

        kerf("unload", NAME, stage="contract-unload")
        _expect_status("ready")
        kerf("delete", NAME, stage="contract-delete")
    finally:
        console.close()


def main() -> int:
    try:
        run()
    except Exception as error:
        emit(f"MK_CONTRACT_FAIL reason={type(error).__name__} detail={error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
