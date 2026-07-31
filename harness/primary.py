"""Primary-guest SR-IOV ownership and lifecycle scenario."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import IO, Sequence

from harness.baseline import render_single_vf_baseline
from harness.console import wait_for_alive
from harness.events import EVENT_PREFIX, encode_marker
from harness.inventory import PciFunction


BUSYBOX = "/bin/busybox"
PCI_DEVICES = Path("/sys/bus/pci/devices")
INSTANCES = Path("/sys/fs/multikernel/instances")
ASSIGNMENT_DRIVER = "multikernel-pci-assignment"


class ScenarioFailure(RuntimeError):
    """A named guest-side proof failure."""


def emit(message: str) -> None:
    print(message, flush=True)
    if not message.startswith("MK_SECONDARY_STREAM:") and EVENT_PREFIX not in message:
        structured = encode_marker(message, "primary")
        if structured is not None:
            print(structured, flush=True)


def command(
    arguments: Sequence[str],
    stage: str,
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(arguments),
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=False,
    )
    if check and result.returncode:
        raise ScenarioFailure(stage)
    return result


def kerf(
    *arguments: str, stage: str, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return command(
        [sys.executable, "-m", "kerf.cli", *arguments],
        stage,
        check=check,
        capture=capture,
    )


def read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def write_text(path: Path, value: str, stage: str) -> None:
    try:
        path.write_text(value)
    except OSError as error:
        raise ScenarioFailure(stage) from error


def expect_write_rejected(path: Path, value: str, stage: str) -> None:
    try:
        path.write_text(value)
    except OSError as error:
        Path(f"/run/{stage}.log").write_text(f"{error}\n")
        return
    raise ScenarioFailure(f"{stage}-accepted")


def dmesg() -> str:
    return command([BUSYBOX, "dmesg"], "dmesg", capture=True).stdout


def require_dmesg(text: str, stage: str) -> None:
    if text not in dmesg():
        raise ScenarioFailure(stage)


def wait_until(predicate, attempts: int = 50) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(1)
    return False


class PrimaryScenario:
    def __init__(self) -> None:
        self.pool_base = ""
        self.pf = PciFunction.from_path(PCI_DEVICES / "0000:00:02.0")
        self.vf: PciFunction | None = None
        self.pf_netdev = ""
        self.vf_host_driver = ""

    @property
    def assigned_vf(self) -> PciFunction:
        if self.vf is None:
            raise ScenarioFailure("vf-not-initialized")
        return self.vf

    def assert_pf_owned(self, phase: str) -> None:
        if self.pf.driver != "igb":
            raise ScenarioFailure(f"pf-driver-{phase}")
        if read_text(self.pf.path / "sriov_numvfs") != "1":
            raise ScenarioFailure(f"vf-count-{phase}")
        result = command(
            [BUSYBOX, "ip", "link", "show", self.pf_netdev],
            f"pf-link-{phase}",
            capture=True,
        )
        if "UP" not in result.stdout:
            raise ScenarioFailure(f"pf-link-{phase}")

    def assert_vf_owner(self, expected: str, phase: str) -> None:
        if self.assigned_vf.driver != expected:
            raise ScenarioFailure(f"vf-owner-{phase}")
        self.assert_pf_owned(phase)

    def expect_status(self, name: str, instance_id: int, expected: str) -> None:
        status_path = INSTANCES / name / "status"
        actual = ""
        for _ in range(50):
            actual = read_text(status_path)
            if actual == expected:
                emit(f"MK_STAGE_STATUS_{expected} name={name} id={instance_id}")
                return
            time.sleep(1)
        emit(
            f"MK_STATUS_MISMATCH name={name} id={instance_id} "
            f"expected={expected} actual={actual}"
        )
        raise ScenarioFailure(f"status-{name}-{expected}")

    def expect_create_rejected(
        self,
        stage: str,
        name: str,
        instance_id: int,
        cpu: int,
        memory: str,
        devices: str,
        expected_driver: str,
    ) -> None:
        result = kerf(
            "create",
            name,
            f"--id={instance_id}",
            f"--cpus={cpu}",
            f"--memory={memory}",
            f"--devices={devices}",
            stage=stage,
            check=False,
            capture=True,
        )
        Path(f"/run/{stage}.log").write_text(result.stdout or "")
        if result.returncode == 0:
            if result.stdout:
                emit(result.stdout)
            raise ScenarioFailure(f"{stage}-accepted")
        if (INSTANCES / name).exists():
            raise ScenarioFailure(f"{stage}-instance-leaked")
        self.assert_vf_owner(expected_driver, stage)
        emit(
            f"MK_HOSTILE_CREATE_REJECTED stage={stage} name={name} "
            f"id={instance_id} devices={devices} vf_driver={expected_driver} "
            "rollback=clean"
        )

    def hostile_lease_attempts(self, phase: str) -> None:
        vf = self.assigned_vf
        expect_write_rejected(
            self.pf.path / "sriov_numvfs",
            "0\n",
            f"disable-vfs-{phase}",
        )
        self.assert_vf_owner(ASSIGNMENT_DRIVER, f"disable-vfs-{phase}")
        emit(f"MK_HOSTILE_VF_DISABLE_REJECTED phase={phase} pf={self.pf.bdf} vfs=1")

        expect_write_rejected(
            Path("/sys/bus/pci/drivers/igbvf/bind"),
            f"{vf.bdf}\n",
            f"rebind-vf-{phase}",
        )
        self.assert_vf_owner(ASSIGNMENT_DRIVER, f"rebind-vf-{phase}")
        emit(
            f"MK_HOSTILE_VF_REBIND_REJECTED phase={phase} vf={vf.bdf} "
            f"owner={ASSIGNMENT_DRIVER}"
        )

        write_text(
            Path("/sys/bus/pci/drivers_probe"),
            f"{vf.bdf}\n",
            f"reprobe-vf-{phase}-write",
        )
        self.assert_vf_owner(ASSIGNMENT_DRIVER, f"reprobe-vf-{phase}")
        emit(
            f"MK_HOSTILE_VF_REPROBE_CONTAINED phase={phase} vf={vf.bdf} "
            f"owner={ASSIGNMENT_DRIVER}"
        )

        expect_write_rejected(
            Path(f"/sys/bus/pci/drivers/{ASSIGNMENT_DRIVER}/bind"),
            f"{self.pf.bdf}\n",
            f"bind-pf-{phase}",
        )
        self.assert_vf_owner(ASSIGNMENT_DRIVER, f"bind-pf-{phase}")
        emit(
            f"MK_HOSTILE_PF_BIND_REJECTED phase={phase} "
            f"pf={self.pf.bdf} driver=igb"
        )

    def allocate_pool(self) -> None:
        command([BUSYBOX, "insmod", "/lib/modules/lazy_cma.ko"], "lazy-cma-module")
        result = command(
            ["/bin/lazy_cma_tool", "-a", "512", "-n", "Multikernel Memory Pool"],
            "pool-allocation",
            capture=True,
        )
        self.pool_base = result.stdout.split()[-1]
        if not self.pool_base.startswith("0x"):
            raise ScenarioFailure("pool-address")

    def prepare_inventory(self) -> None:
        netdevs = sorted((self.pf.path / "net").iterdir())
        if not netdevs:
            raise ScenarioFailure("pf-netdev")
        self.pf_netdev = netdevs[0].name
        command(
            [BUSYBOX, "ip", "link", "set", self.pf_netdev, "up"],
            "pf-link-up",
        )
        command(
            [
                BUSYBOX,
                "ip",
                "addr",
                "add",
                "10.0.2.14/24",
                "dev",
                self.pf_netdev,
            ],
            "pf-address",
        )
        emit(f"MK_STAGE_PF_LINK_UP pf={self.pf.bdf} netdev={self.pf_netdev}")
        emit(
            f"MK_STAGE_PF_ADDRESS pf={self.pf.bdf} netdev={self.pf_netdev} "
            "address=10.0.2.14/24"
        )
        time.sleep(2)
        if not (self.pf.path / "sriov_numvfs").exists():
            raise ScenarioFailure("vf-missing-sriov")
        write_text(self.pf.path / "sriov_numvfs", "1\n", "vf-enable")
        if not wait_until(lambda: self.pf.virtual_function(0) is not None):
            raise ScenarioFailure("vf-discovery")
        self.vf = self.pf.virtual_function(0)
        vf = self.assigned_vf
        Path("/run/baseline.dts").write_text(
            render_single_vf_baseline(self.pool_base, self.pf, vf)
        )
        emit(
            f"MK_STAGE_VF_CREATED pf={self.pf.bdf} vf={vf.bdf} "
            f"vendor={vf.vendor:04x} device={vf.device:04x}"
        )
        if not wait_until(lambda: bool(vf.driver)):
            raise ScenarioFailure("vf-host-driver")
        self.vf_host_driver = vf.driver
        if self.vf_host_driver != "igbvf":
            raise ScenarioFailure("vf-host-driver")
        members = vf.iommu_group_members()
        if members != (vf.bdf,):
            raise ScenarioFailure("vf-iommu-group-not-singleton")
        messages = dmesg()
        if "DMAR: IOMMU enabled" not in messages:
            raise ScenarioFailure("iommu-not-enabled")
        if "DMAR-IR: Enabled IRQ remapping" not in messages:
            raise ScenarioFailure("irq-remapping-not-enabled")
        emit("MK_STAGE_IOMMU_ENABLED driver=intel_iommu strict=1 intremap=on")
        emit(f"MK_STAGE_IOMMU_GROUP vf={vf.bdf} group={vf.iommu_group} members=1")

    def wait_for_secondary(self, console: IO[bytes]) -> None:
        if wait_for_alive(
            {1: console},
            timeout=90,
            on_line=lambda _instance, line: emit(f"MK_SECONDARY_STREAM:{line}"),
        ):
            return
        status = read_text(INSTANCES / "qemu-demo/status")
        cpu_online = read_text(Path("/sys/devices/system/cpu/cpu2/online"))
        emit(f"MK_SECONDARY_TIMEOUT_DIAG id=1 status={status} cpu2_online={cpu_online}")
        for line in dmesg().splitlines()[-80:]:
            emit(line)
        raise ScenarioFailure("secondary-marker")

    def run(self) -> None:
        self.allocate_pool()
        self.prepare_inventory()
        vf = self.assigned_vf
        kerf("init", "--input=/run/baseline.dts", stage="kerf-init")
        if "Multikernel Memory Pool" not in read_text(Path("/proc/iomem")):
            raise ScenarioFailure("pool-handoff")
        emit(f"MK_STAGE_POOL_OK size=512M base={self.pool_base}")
        self.assert_vf_owner(self.vf_host_driver, "baseline")
        emit(f"MK_STAGE_PF_RETAINED pf={self.pf.bdf} driver=igb")
        emit(
            f"MK_STAGE_VF_HOST_OWNED vf={vf.bdf} driver={self.vf_host_driver} "
            "phase=baseline"
        )
        emit("MK_STAGE_KERF_INIT_OK cpus=2,3 memory=512M")
        self.expect_create_rejected(
            "pf-assignment", "hostile-pf", 101, 2, "64MB", "igbpf0", "igbvf"
        )
        require_dmesg(
            f"PCI assignment only supports SR-IOV VFs, rejecting {self.pf.bdf}",
            "pf-rejection-not-kernel-enforced",
        )
        self.expect_create_rejected(
            "duplicate-vf",
            "hostile-duplicate",
            102,
            2,
            "64MB",
            "igbvf0,igbvf0",
            "igbvf",
        )
        kerf(
            "create",
            "qemu-demo",
            "--id=1",
            "--cpus=2",
            "--memory=256MB",
            f"--memory-base={self.pool_base}",
            "--devices=igbvf0",
            stage="kerf-create",
        )
        emit("MK_STAGE_KERF_CREATE_OK id=1")
        self.expect_status("qemu-demo", 1, "ready")
        if self.pf.driver != "igb":
            raise ScenarioFailure("pf-driver-lost-during-assignment")
        if vf.driver != ASSIGNMENT_DRIVER:
            raise ScenarioFailure("vf-assignment-driver")
        require_dmesg(
            f"Prepared host IOMMU domain for {vf.bdf} with 1 instance regions",
            "vf-iommu-domain-prepare",
        )
        require_dmesg(
            f"Attached {vf.bdf} to host-owned IOMMU domain for instance 1",
            "vf-iommu-domain-attach",
        )
        emit(
            f"MK_STAGE_VF_LEASED id=1 vf={vf.bdf} driver={vf.driver} "
            f"host_driver={self.vf_host_driver}"
        )
        emit(
            f"MK_STAGE_IOMMU_DOMAIN id=1 vf={vf.bdf} group={vf.iommu_group} "
            "mapped_regions=1 owner=host"
        )
        self.expect_create_rejected(
            "second-owner",
            "hostile-owner",
            103,
            3,
            "128MB",
            "igbvf0",
            ASSIGNMENT_DRIVER,
        )
        self.hostile_lease_attempts("ready")
        device_tree = (INSTANCES / "qemu-demo/device_tree").read_bytes()
        if b"pci-host-bridges" not in device_tree:
            raise ScenarioFailure("pci-host-bridge-instance-dtb")
        emit(
            "MK_STAGE_PCI_HOST_BRIDGE_METADATA id=1 segment=0000 "
            "buses=00-ff ecam=0xb0000000"
        )
        if vf.bdf.encode("ascii") not in device_tree:
            raise ScenarioFailure("vf-instance-dtb")
        emit(f"MK_STAGE_VF_ASSIGNED id=1 vf={vf.bdf} resource=igbvf0")
        kerf(
            "load",
            "qemu-demo",
            "--kernel=/payload/vmlinux",
            "--initrd=/payload/secondary-initrd.cpio.gz",
            f"--cmdline=rdinit=/init quiet loglevel=6 panic=-1 kho=on mk_vf_bdf={vf.bdf}",
            "--console=mktty0",
            stage="kerf-load",
        )
        emit("MK_STAGE_KERF_LOAD_OK id=1")
        self.expect_status("qemu-demo", 1, "loaded")
        with open("/dev/mktty", "r+b", buffering=0) as console:
            console.write(b"1\n")
            emit("MK_STAGE_MKTTY_CONNECTED id=1")
            kerf("exec", "qemu-demo", stage="kerf-exec")
            emit("MK_STAGE_KERF_EXEC_OK id=1")
            self.expect_status("qemu-demo", 1, "active")
            self.wait_for_secondary(console)
            emit("MK_PRIMARY_STILL_ALIVE instance=0 after=MK_SECONDARY_ALIVE")
            self.assert_vf_owner(ASSIGNMENT_DRIVER, "active")
            emit(
                f"MK_STAGE_PF_ACTIVE pf={self.pf.bdf} driver=igb "
                f"netdev={self.pf_netdev} vfs=1"
            )
            self.hostile_lease_attempts("active")
            kerf("kill", "qemu-demo", stage="kerf-kill")
            emit("MK_STAGE_KERF_KILL_OK id=1")
            self.expect_status("qemu-demo", 1, "loaded")
            if vf.driver != ASSIGNMENT_DRIVER:
                raise ScenarioFailure("vf-lease-released-on-kill")
            emit(
                f"MK_HOSTILE_LEASE_PERSISTED phase=after-kill vf={vf.bdf} "
                f"owner={ASSIGNMENT_DRIVER}"
            )
            kerf("unload", "qemu-demo", stage="kerf-unload")
            emit("MK_STAGE_KERF_UNLOAD_OK id=1")
            self.expect_status("qemu-demo", 1, "ready")
            if vf.driver != ASSIGNMENT_DRIVER:
                raise ScenarioFailure("vf-lease-released-on-unload")
            emit(
                f"MK_HOSTILE_LEASE_PERSISTED phase=after-unload vf={vf.bdf} "
                f"owner={ASSIGNMENT_DRIVER}"
            )
        kerf("delete", "qemu-demo", stage="kerf-delete")
        emit("MK_STAGE_KERF_DELETE_OK id=1")
        if (INSTANCES / "qemu-demo").exists():
            raise ScenarioFailure("instance-not-deleted")
        if not wait_until(lambda: vf.driver == self.vf_host_driver):
            raise ScenarioFailure("vf-host-driver-not-restored")
        self.assert_pf_owned("cycle-1")
        emit(
            f"MK_STAGE_VF_RESTORED phase=cycle-1 vf={vf.bdf} "
            f"driver={self.vf_host_driver} pf={self.pf.bdf} pf_driver=igb"
        )
        for cycle in range(2, 5):
            name = f"qemu-repeat-{cycle}"
            kerf(
                "create",
                name,
                f"--id={cycle}",
                "--cpus=2",
                "--memory=256MB",
                f"--memory-base={self.pool_base}",
                "--devices=igbvf0",
                stage=f"repeat-create-{cycle}",
            )
            self.expect_status(name, cycle, "ready")
            self.assert_vf_owner(ASSIGNMENT_DRIVER, f"repeat-{cycle}")
            require_dmesg(
                f"Attached {vf.bdf} to host-owned IOMMU domain for instance {cycle}",
                f"repeat-iommu-{cycle}",
            )
            emit(f"MK_REPEAT_VF_LEASED cycle={cycle} vf={vf.bdf} group={vf.iommu_group}")
            kerf("delete", name, stage=f"repeat-delete-{cycle}")
            if (INSTANCES / name).exists():
                raise ScenarioFailure(f"repeat-instance-{cycle}")
            if not wait_until(lambda: vf.driver == self.vf_host_driver):
                raise ScenarioFailure(f"repeat-restore-{cycle}")
            self.assert_pf_owned(f"repeat-restore-{cycle}")
            emit(f"MK_REPEAT_CYCLE_PASS cycle={cycle} vf={vf.bdf} restoration=verified")
        kerf(
            "create",
            "hostile-unbind",
            "--id=104",
            "--cpus=2",
            "--memory=256MB",
            f"--memory-base={self.pool_base}",
            "--devices=igbvf0",
            stage="hostile-unbind-create",
        )
        self.expect_status("hostile-unbind", 104, "ready")
        self.assert_vf_owner(ASSIGNMENT_DRIVER, "hostile-unbind-ready")
        write_text(
            Path(f"/sys/bus/pci/drivers/{ASSIGNMENT_DRIVER}/unbind"),
            f"{vf.bdf}\n",
            "hostile-unbind-write",
        )
        self.expect_status("hostile-unbind", 104, "failed")
        if vf.driver:
            raise ScenarioFailure("hostile-unbind-driver-present")
        require_dmesg(
            f"PCI assignment lease for {vf.bdf} was lost by instance 104",
            "hostile-unbind-no-lease-loss",
        )
        self.assert_pf_owned("hostile-unbind-failed")
        emit(
            f"MK_HOSTILE_SURPRISE_UNBIND_FAIL_CLOSED id=104 vf={vf.bdf} "
            "state=failed vf_driver=unbound pf_driver=igb"
        )
        kerf("delete", "hostile-unbind", stage="hostile-unbind-delete")
        if not wait_until(lambda: vf.driver == self.vf_host_driver):
            raise ScenarioFailure("hostile-unbind-restore")
        self.assert_pf_owned("hostile-unbind-restored")
        emit(
            f"MK_HOSTILE_SURPRISE_UNBIND_RECOVERED id=104 vf={vf.bdf} "
            f"driver={self.vf_host_driver}"
        )
        write_text(self.pf.path / "sriov_numvfs", "0\n", "vf-teardown")
        emit(f"MK_STAGE_VF_TEARDOWN pf={self.pf.bdf} vfs=0")
        emit(
            "MK_DEMO_PASS simultaneous_kernels=verified iommu=verified "
            "vf_lease=verified hostile_attempts=verified repeat_cycles=3 "
            "fail_closed=verified"
        )


def shutdown() -> None:
    command([BUSYBOX, "sync"], "sync", check=False)
    command([BUSYBOX, "poweroff", "-f"], "poweroff", check=False)
    while True:
        time.sleep(1)


def main() -> int:
    try:
        PrimaryScenario().run()
    except ScenarioFailure as error:
        emit(f"MK_DEMO_FAIL stage={error}")
    except Exception:
        traceback.print_exc()
        emit("MK_DEMO_FAIL stage=python-exception")
    shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
