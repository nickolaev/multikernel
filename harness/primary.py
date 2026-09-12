"""Primary-guest SR-IOV ownership and lifecycle scenario."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Sequence

from harness.baseline import PciResource, render_baseline
from harness.console import ConsoleEventReader, wait_for_alive
from harness.events import EVENT_PREFIX, encode_marker
from harness.inventory import PciFunction
from harness.topology import PCI_FAMILIES, PciFamily


BUSYBOX = "/bin/busybox"
PCI_DEVICES = Path("/sys/bus/pci/devices")
INSTANCES = Path("/sys/fs/multikernel/instances")
ASSIGNMENT_DRIVER = "multikernel-pci-assignment"


class ScenarioFailure(RuntimeError):
    """A named guest-side proof failure."""


def emit(message: str) -> None:
    os.write(sys.stdout.fileno(), f"{message}\n".encode("ascii", errors="replace"))
    if not message.startswith("MK_SECONDARY_STREAM") and EVENT_PREFIX not in message:
        structured = encode_marker(message, "primary")
        if structured is not None:
            os.write(
                sys.stdout.fileno(),
                f"{structured}\n".encode("ascii", errors="replace"),
            )


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
        cpu_count = os.cpu_count() or 0
        self.pool_cpus = tuple(range(2, cpu_count))
        if len(self.pool_cpus) < 3:
            raise ScenarioFailure("insufficient-pool-cpus")
        self.pf = PciFunction.from_path(PCI_DEVICES / "0000:00:02.0")
        self.vf: PciFunction | None = None
        self.pf_netdev = ""
        self.vf_host_driver = ""
        self.family_pfs: dict[str, PciFunction] = {}
        self.family_vfs: dict[str, tuple[PciFunction, ...]] = {}
        self.family_netdevs: dict[str, str] = {}

    @property
    def assigned_vf(self) -> PciFunction:
        if self.vf is None:
            raise ScenarioFailure("vf-not-initialized")
        return self.vf

    def assert_pf_owned(self, phase: str) -> None:
        if self.pf.driver != "igb":
            raise ScenarioFailure(f"pf-driver-{phase}")
        if read_text(self.pf.path / "sriov_numvfs") != "4":
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
        emit(f"MK_HOSTILE_VF_DISABLE_REJECTED phase={phase} pf={self.pf.bdf} vfs=4")

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
            ["/bin/lazy_cma_tool", "-a", "1024", "-n", "Multikernel Memory Pool"],
            "pool-allocation",
            capture=True,
        )
        self.pool_base = result.stdout.split()[-1]
        if not self.pool_base.startswith("0x"):
            raise ScenarioFailure("pool-address")

    def configure_pf_network(self, family: PciFamily, pf: PciFunction) -> None:
        netdevs = sorted((pf.path / "net").iterdir())
        if not netdevs:
            raise ScenarioFailure(f"pf-netdev-{family.name}")
        netdev = netdevs[0].name
        self.family_netdevs[family.name] = netdev
        command([BUSYBOX, "ip", "link", "set", netdev, "up"], f"pf-link-{family.name}")
        command(
            [BUSYBOX, "ip", "addr", "add", family.primary_address, "dev", netdev],
            f"pf-address-{family.name}",
        )
        emit(f"MK_COMPLEX_PF_LINK_UP family={family.name} pf={pf.bdf} netdev={netdev}")
        if family.name == "igb0":
            self.pf_netdev = netdev
            emit(f"MK_STAGE_PF_LINK_UP pf={pf.bdf} netdev={netdev}")
            emit(
                f"MK_STAGE_PF_ADDRESS pf={pf.bdf} netdev={netdev} "
                f"address={family.primary_address}"
            )

    def prepare_inventory(self) -> None:
        resources: list[PciResource] = []
        for family in PCI_FAMILIES:
            pf = PciFunction.from_path(PCI_DEVICES / family.pf_bdf)
            if pf.driver != family.pf_driver:
                raise ScenarioFailure(f"pf-driver-{family.name}")
            self.family_pfs[family.name] = pf
            self.configure_pf_network(family, pf)
            if not (pf.path / "sriov_numvfs").exists():
                raise ScenarioFailure(f"vf-missing-sriov-{family.name}")
            write_text(
                pf.path / "sriov_numvfs",
                f"{family.vf_count}\n",
                f"vf-enable-{family.name}",
            )
            if not wait_until(
                lambda pf=pf, family=family: all(
                    pf.virtual_function(index) is not None
                    for index in range(family.vf_count)
                )
            ):
                raise ScenarioFailure(f"vf-discovery-{family.name}")
            vfs = tuple(
                pf.virtual_function(index) for index in range(family.vf_count)
            )
            if any(vf is None for vf in vfs):
                raise ScenarioFailure(f"vf-discovery-{family.name}")
            typed_vfs = tuple(vf for vf in vfs if vf is not None)
            self.family_vfs[family.name] = typed_vfs
            resources.append(PciResource(family.pf_resource, family.compatible_pf, pf))
            for index, vf in enumerate(typed_vfs):
                if not wait_until(lambda vf=vf: bool(vf.driver)):
                    raise ScenarioFailure(f"vf-host-driver-{family.name}-{index}")
                if vf.driver != family.vf_driver:
                    raise ScenarioFailure(f"vf-host-driver-{family.name}-{index}")
                if vf.iommu_group_members() != (vf.bdf,):
                    raise ScenarioFailure(f"vf-iommu-group-{family.name}-{index}")
                resources.append(
                    PciResource(
                        f"{family.vf_resource_prefix}{index}",
                        family.compatible_vf,
                        vf,
                    )
                )
                emit(
                    f"MK_COMPLEX_VF_HOST_OWNED family={family.name} index={index} "
                    f"vf={vf.bdf} driver={vf.driver} group={vf.iommu_group}"
                )
            emit(
                f"MK_COMPLEX_PF_READY family={family.name} pf={pf.bdf} "
                f"driver={pf.driver} vfs={family.vf_count}"
            )
        self.pf = self.family_pfs["igb0"]
        self.vf = self.family_vfs["igb0"][0]
        self.vf_host_driver = self.assigned_vf.driver
        Path("/run/baseline.dts").write_text(
            render_baseline(
                self.pool_base,
                cpus=self.pool_cpus,
                memory_bytes=0x40000000,
                resources=resources,
            )
        )
        vf = self.assigned_vf
        emit(
            f"MK_STAGE_VF_CREATED pf={self.pf.bdf} vf={vf.bdf} "
            f"vendor={vf.vendor:04x} device={vf.device:04x}"
        )
        messages = dmesg()
        if "DMAR: IOMMU enabled" not in messages:
            raise ScenarioFailure("iommu-not-enabled")
        if "DMAR-IR: Enabled IRQ remapping" not in messages:
            raise ScenarioFailure("irq-remapping-not-enabled")
        for noise_bdf in ("0000:00:05.0", "0000:00:06.0"):
            if not (PCI_DEVICES / noise_bdf).exists():
                raise ScenarioFailure(f"pci-noise-missing-{noise_bdf}")
        emit("MK_STAGE_IOMMU_ENABLED driver=intel_iommu strict=1 intremap=on")
        emit(f"MK_STAGE_IOMMU_GROUP vf={vf.bdf} group={vf.iommu_group} members=1")
        emit("MK_COMPLEX_TOPOLOGY_READY pfs=3 vfs=8 assigned=0 noise=2")

    @staticmethod
    def _config_progress_reads(event: dict[str, object]) -> int:
        fields = event.get("fields")
        if not isinstance(fields, dict) or str(fields.get("instance")) != "1":
            return -1
        try:
            return int(fields.get("reads", -1))
        except (TypeError, ValueError):
            return -1

    def exercise_concurrent_cpu_config(self, console: IO[bytes]) -> None:
        progress_event = "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS"
        reader = ConsoleEventReader(
            console,
            on_line=lambda line: emit(f"MK_SECONDARY_STREAM:{line}"),
        )
        baseline = reader.wait_for_event(
            progress_event,
            timeout=60,
            predicate=lambda event: self._config_progress_reads(event) >= 0,
        )
        if baseline is None:
            raise ScenarioFailure("concurrent-cpu-config-baseline")
        try:
            queued_baseline = reader.drain_for_event(
                progress_event,
                predicate=lambda event: self._config_progress_reads(event) >= 0,
                idle_timeout=0.25,
                timeout=5,
            )
        except TimeoutError as error:
            raise ScenarioFailure("concurrent-cpu-config-baseline-drain") from error
        if queued_baseline is not None:
            baseline = queued_baseline
        baseline_reads = self._config_progress_reads(baseline)

        expanded = ",".join(str(cpu) for cpu in self.pool_cpus)
        for cycle in range(1, 4):
            kerf(
                "update",
                "qemu-demo",
                f"--cpus={expanded}",
                stage=f"config-hotplug-expand-{cycle}",
            )
            self.expect_status("qemu-demo", 1, "active")
            kerf(
                "update",
                "qemu-demo",
                "--cpus=2",
                stage=f"config-hotplug-contract-{cycle}",
            )
            self.expect_status("qemu-demo", 1, "active")

        try:
            backlog = reader.drain_for_event(
                progress_event,
                predicate=lambda event: self._config_progress_reads(event) >= 0,
                idle_timeout=0.25,
                timeout=5,
            )
        except TimeoutError as error:
            raise ScenarioFailure("concurrent-cpu-config-backlog-drain") from error
        backlog_reads = (
            self._config_progress_reads(backlog)
            if backlog is not None
            else baseline_reads
        )
        watermark = max(baseline_reads, backlog_reads)
        progress = reader.wait_for_event(
            progress_event,
            timeout=60,
            predicate=lambda event: self._config_progress_reads(event) > watermark,
        )
        if progress is None:
            raise ScenarioFailure("concurrent-cpu-config-progress")
        progress_reads = self._config_progress_reads(progress)

        messages = dmesg()
        for marker in (
            "IPI ring buffer full",
            "PCI config request timed out",
            "PCI IRQ request timed out",
        ):
            if marker in messages:
                raise ScenarioFailure("concurrent-cpu-config-kernel-error")
        emit(
            "MK_CONCURRENT_CPU_PCI_RPC_PASS "
            f"cycles=3 max_cpus={len(self.pool_cpus)} "
            f"baseline_reads={baseline_reads} backlog_reads={watermark} "
            f"progress_reads={progress_reads}"
        )

    def wait_for_secondary(self, console: IO[bytes]) -> None:
        if wait_for_alive(
            {1: console},
            timeout=180,
            on_line=lambda _instance, line: emit(f"MK_SECONDARY_STREAM:{line}"),
        ):
            return
        status = read_text(INSTANCES / "qemu-demo/status")
        cpu_online = read_text(Path("/sys/devices/system/cpu/cpu2/online"))
        emit(f"MK_SECONDARY_TIMEOUT_DIAG id=1 status={status} cpu2_online={cpu_online}")
        for line in dmesg().splitlines()[-80:]:
            emit(line)
        raise ScenarioFailure("secondary-marker")

    @staticmethod
    def _secondary_cmdline(
        family: PciFamily, instance_id: int, vf: PciFunction
    ) -> str:
        return (
            "rdinit=/init quiet loglevel=6 panic=-1 tsc=reliable "
            f"mk_instance_id={instance_id} mk_pf_bdf={family.pf_bdf} "
            f"mk_vf_bdf={vf.bdf} mk_vf_vendor=0x{vf.vendor:04x} "
            f"mk_vf_device=0x{vf.device:04x} mk_vf_driver={family.vf_driver} "
            f"mk_vf_address={family.subnet}.15/24 "
            f"mk_vf_peer={family.subnet}.2 mk_primary_peer={family.subnet}.14"
        )

    @staticmethod
    def _open_console(instance_id: int) -> IO[bytes]:
        """Attach one mktty fd to an instance, as kerf console does."""
        console = open("/dev/mktty", "r+b", buffering=0)
        try:
            console.write(f"{instance_id}\n".encode("ascii"))
        except OSError:
            console.close()
            raise
        return console

    def expect_complex_rejected(
        self,
        stage: str,
        name: str,
        instance_id: int,
        cpu: int,
        resource: str,
    ) -> None:
        result = kerf(
            "create",
            name,
            f"--id={instance_id}",
            f"--cpus={cpu}",
            "--memory=64MB",
            f"--devices={resource}",
            stage=stage,
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            raise ScenarioFailure(f"{stage}-accepted")
        if (INSTANCES / name).exists():
            raise ScenarioFailure(f"{stage}-instance-leaked")

    def run_complex_peers(self, primary_console: IO[bytes]) -> None:
        third = PCI_FAMILIES[2]
        self.expect_complex_rejected(
            "complex-igb2-pf",
            "complex-igb2-pf",
            120,
            4,
            third.pf_resource,
        )
        third_pf = self.family_pfs[third.name]
        if third_pf.driver != third.pf_driver:
            raise ScenarioFailure("complex-igb2-pf-driver")
        require_dmesg(
            f"PCI assignment only supports SR-IOV VFs, rejecting {third_pf.bdf}",
            "complex-igb2-pf-kernel-rejection",
        )
        emit(
            f"MK_COMPLEX_PF_REJECTED family={third.name} pf={third_pf.bdf} "
            f"driver={third_pf.driver}"
        )

        cases = (
            (PCI_FAMILIES[1], "complex-igb1", 2, 3),
            (PCI_FAMILIES[2], "complex-igb2", 3, 4),
        )
        for family, name, instance_id, cpu in cases:
            vf = self.family_vfs[family.name][0]
            kerf(
                "create",
                name,
                f"--id={instance_id}",
                f"--cpus={cpu}",
                "--memory=256MB",
                f"--devices={family.vf_resource_prefix}0",
                stage=f"complex-create-{family.name}",
            )
            self.expect_status(name, instance_id, "ready")
            if vf.driver != ASSIGNMENT_DRIVER:
                raise ScenarioFailure(f"complex-owner-{family.name}")
            if self.family_pfs[family.name].driver != family.pf_driver:
                raise ScenarioFailure(f"complex-pf-driver-{family.name}")
            require_dmesg(
                f"Attached {vf.bdf} to host-owned IOMMU domain for instance {instance_id}",
                f"complex-iommu-{family.name}",
            )
            emit(
                f"MK_COMPLEX_INSTANCE_READY family={family.name} id={instance_id} "
                f"cpu={cpu} vf={vf.bdf} group={vf.iommu_group}"
            )

        # Every loaded peer gets its own mktty attachment.  The initial ID
        # write selects the endpoint before the guest is started, just as
        # `kerf console --id=...` does for an already active instance.
        with ExitStack() as consoles:
            peer_consoles: dict[int, IO[bytes]] = {}
            for family, name, instance_id, _cpu, _memory_offset in cases:
                vf = self.family_vfs[family.name][0]
                kerf(
                    "load",
                    name,
                    "--kernel=/payload/vmlinux",
                    "--initrd=/payload/secondary-initrd.cpio.gz",
                    f"--cmdline={self._secondary_cmdline(family, instance_id, vf)}",
                    "--console=mktty0",
                    stage=f"complex-load-{family.name}",
                )
                self.expect_status(name, instance_id, "loaded")
                console = consoles.enter_context(self._open_console(instance_id))
                peer_consoles[instance_id] = console
                kerf("exec", name, stage=f"complex-exec-{family.name}")
                self.expect_status(name, instance_id, "active")

            if not wait_for_alive(
                peer_consoles,
                timeout=180,
                on_line=lambda instance, line: emit(
                    f"MK_SECONDARY_STREAM instance={instance}:{line}"
                ),
            ):
                raise ScenarioFailure("complex-secondary-marker")
            # The primary console was already drained by wait_for_secondary;
            # reusing that fd would race the stream and reopening it would
            # select a second endpoint after the primary's alive event.
            self.expect_status("qemu-demo", 1, "active")
            self.assert_vf_owner(ASSIGNMENT_DRIVER, "complex-concurrent-leases")
            emit(
                "MK_COMPLEX_CONCURRENT_LEASES_PASS leases=3 active_instances=3 "
                "families=igb0,igb1,igb2"
            )

            for family, name, instance_id, _cpu, _memory_offset in cases:
                self.expect_status(name, instance_id, "active")
                emit(f"MK_COMPLEX_INSTANCE_ACTIVE family={family.name} id={instance_id}")

        unassigned = 0
        for family in PCI_FAMILIES:
            for vf in self.family_vfs[family.name][1:]:
                if vf.driver != family.vf_driver:
                    raise ScenarioFailure(f"complex-unassigned-owner-{family.name}")
                unassigned += 1
        emit(
            f"MK_COMPLEX_UNASSIGNED_VFS_INTACT count={unassigned} "
            "families=3 owner=host"
        )

        emit(
            f"MK_COMPLEX_HOSTILE_CONTAINED family=igb0 id=1 "
            f"vf={self.assigned_vf.bdf} owner={self.assigned_vf.driver}"
        )
        for family, _name, instance_id, _cpu, _memory_offset in cases:
            pf = self.family_pfs[family.name]
            vf = self.family_vfs[family.name][0]
            expect_write_rejected(
                pf.path / "sriov_numvfs",
                "0\n",
                f"complex-disable-{family.name}",
            )
            expect_write_rejected(
                Path(f"/sys/bus/pci/drivers/{family.vf_driver}/bind"),
                f"{vf.bdf}\n",
                f"complex-rebind-{family.name}",
            )
            write_text(
                Path("/sys/bus/pci/drivers_probe"),
                f"{vf.bdf}\n",
                f"complex-reprobe-{family.name}",
            )
            if vf.driver != ASSIGNMENT_DRIVER:
                raise ScenarioFailure(f"complex-hostile-owner-{family.name}")
            emit(
                f"MK_COMPLEX_HOSTILE_CONTAINED family={family.name} "
                f"id={instance_id} vf={vf.bdf} owner={vf.driver}"
            )

        for family, name, instance_id, _cpu, _memory_offset in cases:
            kerf("kill", name, stage=f"complex-kill-{family.name}")
            self.expect_status(name, instance_id, "loaded")
            kerf("unload", name, stage=f"complex-unload-{family.name}")
            self.expect_status(name, instance_id, "ready")
            kerf("delete", name, stage=f"complex-delete-{family.name}")
            if (INSTANCES / name).exists():
                raise ScenarioFailure(f"complex-delete-{family.name}")
            vf = self.family_vfs[family.name][0]
            if not wait_until(lambda vf=vf, family=family: vf.driver == family.vf_driver):
                raise ScenarioFailure(f"complex-restore-{family.name}")
            if self.family_pfs[family.name].driver != family.pf_driver:
                raise ScenarioFailure(f"complex-pf-restore-{family.name}")
            emit(
                f"MK_COMPLEX_PEER_TEARDOWN_PASS family={family.name} "
                f"id={instance_id} state=ready-before-delete"
            )
        self.expect_status("qemu-demo", 1, "active")
        self.assert_vf_owner(ASSIGNMENT_DRIVER, "complex-peers-restored")
        emit("MK_COMPLEX_PEERS_RESTORED peers=2 primary_state=active")

    def run_respawn_stress(self) -> None:
        """Create and fully tear down 100 real VF-backed instance lifecycles."""
        vf = self.assigned_vf
        for cycle in range(100):
            name = f"qemu-respawn-{cycle}"
            instance_id = 200 + cycle
            kerf(
                "create",
                name,
                f"--id={instance_id}",
                "--cpus=2",
                "--memory=64MB",
                "--devices=igbvf0",
                stage=f"respawn-create-{cycle}",
            )
            self.expect_status(name, instance_id, "ready")
            self.assert_vf_owner(ASSIGNMENT_DRIVER, f"respawn-assigned-{cycle}")
            kerf("delete", name, stage=f"respawn-delete-{cycle}")
            if (INSTANCES / name).exists():
                raise ScenarioFailure(f"respawn-instance-{cycle}")
            if not wait_until(lambda: vf.driver == self.vf_host_driver):
                raise ScenarioFailure(f"respawn-restore-{cycle}")
            self.assert_pf_owned(f"respawn-restored-{cycle}")
        emit("MK_RESPAWN_STRESS_PASS cycles=100")

    def run(self) -> None:
        self.allocate_pool()
        self.prepare_inventory()
        vf = self.assigned_vf
        kerf("init", "--input=/run/baseline.dts", stage="kerf-init")
        if "Multikernel Memory Pool" not in read_text(Path("/proc/iomem")):
            raise ScenarioFailure("pool-handoff")
        emit(f"MK_STAGE_POOL_OK size=1024M base={self.pool_base}")
        for family in PCI_FAMILIES:
            pf = self.family_pfs[family.name]
            write_text(
                Path("/sys/bus/pci/drivers_probe"),
                f"{pf.bdf}\n",
                f"pf-host-rebind-{family.name}",
            )
            if not wait_until(lambda pf=pf, family=family: pf.driver == family.pf_driver):
                raise ScenarioFailure(f"pf-host-rebind-{family.name}")
            netdevs = sorted((pf.path / "net").iterdir())
            if not netdevs:
                raise ScenarioFailure(f"pf-netdev-rebind-{family.name}")
            netdev = netdevs[0].name
            self.family_netdevs[family.name] = netdev
            if family.name == "igb0":
                self.pf_netdev = netdev
            command([BUSYBOX, "ip", "link", "set", netdev, "up"], f"pf-link-rebind-{family.name}")
            command(
                [BUSYBOX, "ip", "addr", "add", family.primary_address, "dev", netdev],
                f"pf-address-rebind-{family.name}",
            )
            write_text(
                pf.path / "sriov_numvfs",
                f"{family.vf_count}\n",
                f"vf-recreate-{family.name}",
            )
            if not wait_until(
                lambda pf=pf, family=family: all(
                    pf.virtual_function(index) is not None
                    for index in range(family.vf_count)
                )
            ):
                raise ScenarioFailure(f"vf-recreate-{family.name}")
            for index, vf in enumerate(self.family_vfs[family.name]):
                write_text(
                    Path("/sys/bus/pci/drivers_probe"),
                    f"{vf.bdf}\n",
                    f"vf-host-rebind-{family.name}-{index}",
                )
                if not wait_until(lambda vf=vf, family=family: vf.driver == family.vf_driver):
                    raise ScenarioFailure(f"vf-host-rebind-{family.name}-{index}")
        emit("MK_COMPLEX_VF_HOST_REBOUND families=3 vfs=8")
        self.assert_vf_owner(self.vf_host_driver, "baseline")
        emit(f"MK_STAGE_PF_RETAINED pf={self.pf.bdf} driver=igb")
        emit(
            f"MK_STAGE_VF_HOST_OWNED vf={vf.bdf} driver={self.vf_host_driver} "
            "phase=baseline"
        )
        emit(
            "MK_STAGE_KERF_INIT_OK "
            f"cpus={','.join(str(cpu) for cpu in self.pool_cpus)} memory=1024M"
        )
        self.expect_create_rejected(
            "pf-assignment", "hostile-pf", 101, 2, "64MB", "igbpf0", "igbvf"
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
        emit(
            f"MK_COMPLEX_SECOND_OWNER_REJECTED family=igb0 vf={vf.bdf} "
            f"owner={ASSIGNMENT_DRIVER} rollback=clean"
        )
        emit(
            f"MK_COMPLEX_INSTANCE_READY family=igb0 id=1 cpu=2 "
            f"vf={vf.bdf} group={vf.iommu_group}"
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
        # TCG can delay an isolated vCPU long enough for the jiffies
        # watchdog to reject QEMU's otherwise stable shared TSC.
        kerf(
            "load",
            "qemu-demo",
            "--kernel=/payload/vmlinux",
            "--initrd=/payload/secondary-initrd.cpio.gz",
            f"--cmdline=rdinit=/init quiet loglevel=6 panic=-1 "
            f"tsc=reliable mk_vf_bdf={vf.bdf}",
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
            emit("MK_COMPLEX_INSTANCE_ACTIVE family=igb0 id=1")
            self.exercise_concurrent_cpu_config(console)
            self.run_complex_peers(console)
            kerf("kill", "qemu-demo", stage="kerf-kill")
            emit("MK_STAGE_KERF_KILL_OK id=1")
            self.expect_status("qemu-demo", 1, "loaded")
            require_dmesg(
                "Quiesced 3 host-owned PCI IRQ vectors for instance 1",
                "halted-pci-irqs-not-quiesced",
            )
            emit("MK_HALTED_IRQ_QUIESCE_PASS instance=1 vectors=3")
            if vf.driver != ASSIGNMENT_DRIVER:
                raise ScenarioFailure("vf-lease-released-on-kill")
            emit(
                f"MK_HOSTILE_LEASE_PERSISTED phase=after-kill vf={vf.bdf} "
                f"owner={ASSIGNMENT_DRIVER}"
            )
            kerf("exec", "qemu-demo", stage="kerf-restart-exec")
            self.expect_status("qemu-demo", 1, "active")
            self.wait_for_secondary(console)
            self.assert_vf_owner(ASSIGNMENT_DRIVER, "restart-active")
            emit(
                f"MK_RESTART_VF_DATAPATH_PASS instance=1 vf={vf.bdf} "
                "reset=verified traffic=verified"
            )
            kerf("kill", "qemu-demo", stage="kerf-restart-kill")
            self.expect_status("qemu-demo", 1, "loaded")
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
        for family in PCI_FAMILIES:
            for family_vf in self.family_vfs[family.name]:
                if family_vf.driver != family.vf_driver:
                    raise ScenarioFailure(f"complex-unleased-vf-{family.name}")
        emit("MK_COMPLEX_RESTORED families=3 vfs=8 ownership=host")
        for cycle in range(2, 5):
            name = f"qemu-repeat-{cycle}"
            kerf(
                "create",
                name,
                f"--id={cycle}",
                "--cpus=2",
                "--memory=256MB",
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
        self.run_respawn_stress()
        kerf(
            "create",
            "hostile-unbind",
            "--id=104",
            "--cpus=2",
            "--memory=256MB",
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
        for family in reversed(PCI_FAMILIES):
            pf = self.family_pfs[family.name]
            write_text(
                pf.path / "sriov_numvfs",
                "0\n",
                f"vf-teardown-{family.name}",
            )
            emit(
                f"MK_STAGE_VF_TEARDOWN pf={pf.bdf} vfs=0 family={family.name}"
            )
        emit(
            "MK_DEMO_PASS simultaneous_kernels=verified iommu=verified "
            "vf_lease=verified hostile_attempts=verified repeat_cycles=3 "
            "fail_closed=verified complex_topology=verified "
            "concurrent_leases=3 active_instances=3 respawn_cycles=100"
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
