"""Secondary-kernel PCI isolation and VF datapath agent."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import threading
import time

from harness.events import encode_event, format_marker


DEFAULTS = {
    "mk_instance_id": "1",
    "mk_pf_bdf": "0000:00:02.0",
    "mk_vf_vendor": "0x8086",
    "mk_vf_device": "0x10ca",
    "mk_vf_driver": "igbvf",
    "mk_vf_bar": "0",
    "mk_vf_address": "10.0.2.15/24",
    "mk_vf_peer": "10.0.2.2",
    "mk_primary_peer": "10.0.2.14",
}
PCI_CONFIG_PROGRESS_INTERVAL = 1024
BUSYBOX_TIMEOUT_SECONDS = 15
LINK_UP_TIMEOUT_SECONDS = 30
RELIABILITY_COUNTERS = (
    "rx_errors",
    "rx_dropped",
    "rx_fifo_errors",
    "rx_frame_errors",
    "rx_length_errors",
    "rx_missed_errors",
    "tx_errors",
    "tx_dropped",
    "tx_fifo_errors",
    "tx_carrier_errors",
    "tx_heartbeat_errors",
    "tx_window_errors",
    "collisions",
)


def parse_cmdline(text: str) -> dict[str, str]:
    """Return multikernel test parameters from a kernel command line."""
    values = dict(DEFAULTS)
    for argument in text.split():
        key, separator, value = argument.partition("=")
        if separator and key.startswith("mk_"):
            values[key] = value
    return values


def parse_bar(line: str) -> tuple[str, str, str]:
    """Parse one line from a PCI resource file."""
    fields = line.split()
    if len(fields) < 3:
        raise ValueError("PCI resource line has fewer than three fields")
    return fields[0], fields[1], fields[2]


def pci_scope(devices: Path, vf_bdf: str) -> tuple[int, str]:
    """Return the visible PCI function count and the last unexpected BDF."""
    visible = sorted(entry.name for entry in devices.iterdir())
    unexpected = [bdf for bdf in visible if bdf != vf_bdf]
    return len(visible), unexpected[-1] if unexpected else ""


def pci_identity_matches(data: bytes, vendor: str, device: str) -> bool:
    """Return whether a config-space header matches the expected PCI ID."""
    if len(data) != 4:
        return False
    return (
        int.from_bytes(data[0:2], "little") == int(vendor, 0)
        and int.from_bytes(data[2:4], "little") == int(device, 0)
    )


class SecondaryConfig:
    def __init__(self, values: dict[str, str]):
        self.instance = int(values["mk_instance_id"])
        self.pf_bdf = values["mk_pf_bdf"]
        self.vf_bdf = values.get("mk_vf_bdf", "")
        self.vendor = values["mk_vf_vendor"]
        self.device = values["mk_vf_device"]
        self.driver = values["mk_vf_driver"]
        self.bar = int(values["mk_vf_bar"])
        self.address = values["mk_vf_address"]
        self.peer = values["mk_vf_peer"]
        self.primary_peer = values["mk_primary_peer"]

    @classmethod
    def from_path(cls, path: Path) -> SecondaryConfig:
        return cls(parse_cmdline(path.read_text()))


class MarkerSink:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def open(self) -> None:
        self.fd = os.open(self.path, os.O_WRONLY)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def emit(self, event: str, **fields: object) -> None:
        if self.fd is None:
            raise RuntimeError("marker sink is not open")
        lines = (
            encode_event(event, fields, "secondary"),
            format_marker(event, fields),
        )
        for line in lines:
            os.write(self.fd, f"{line}\n".encode("ascii", errors="replace"))


class PciConfigStress:
    """Continuously exercise mediated config access during host operations."""

    def __init__(self, config: SecondaryConfig, sink: MarkerSink, path: Path):
        self.config = config
        self.sink = sink
        self.path = path
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="mk-pci-config-stress",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(15):
            raise RuntimeError("PCI config stress did not become ready")

    def _run(self) -> None:
        consecutive_errors = 0
        reads = 0
        try:
            fd = os.open(self.path, os.O_RDONLY)
            try:
                while True:
                    try:
                        data = os.pread(fd, 4, 0)
                    except OSError:
                        data = b""
                    if not pci_identity_matches(
                        data, self.config.vendor, self.config.device
                    ):
                        consecutive_errors += 1
                        if consecutive_errors >= 100:
                            raise RuntimeError(
                                "PCI config access did not recover after CPU migration"
                            )
                        time.sleep(0.01)
                        continue
                    consecutive_errors = 0
                    reads += 1
                    if reads == 64:
                        self.sink.emit(
                            "MK_SECONDARY_PCI_CONFIG_STRESS_READY",
                            instance=self.config.instance,
                            reads=reads,
                        )
                        self.ready.set()
                    if reads % PCI_CONFIG_PROGRESS_INTERVAL == 0:
                        self.sink.emit(
                            "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS",
                            instance=self.config.instance,
                            reads=reads,
                        )
                    time.sleep(0.001)
            finally:
                os.close(fd)
        except Exception as error:
            self.ready.set()
            self.sink.emit(
                "MK_SECONDARY_FAIL",
                instance=self.config.instance,
                reason="pci-config-stress",
                reads=reads,
                error=type(error).__name__,
            )


class SecondaryScenario:
    def __init__(
        self,
        config: SecondaryConfig,
        sink: MarkerSink,
        sysfs: Path = Path("/sys"),
    ):
        self.config = config
        self.sink = sink
        self.sysfs = sysfs
        self.config_stress: PciConfigStress | None = None

    def fail(self, reason: str, **fields: object) -> None:
        self.sink.emit(
            "MK_SECONDARY_FAIL",
            instance=self.config.instance,
            reason=reason,
            **fields,
        )

    @staticmethod
    def read(path: Path) -> str:
        return path.read_text().strip()

    @staticmethod
    def driver_name(vf_path: Path) -> str:
        driver = vf_path / "driver"
        if not driver.exists():
            return ""
        return os.path.basename(os.path.realpath(driver))

    @staticmethod
    def netdev_name(vf_path: Path) -> str:
        net_dir = vf_path / "net"
        if not net_dir.exists():
            return ""
        entries = sorted(net_dir.iterdir())
        return entries[0].name if entries else ""

    @staticmethod
    def run_busybox(*arguments: str) -> bool:
        try:
            result = subprocess.run(
                ["/bin/busybox", *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=BUSYBOX_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0

    def wait_for_carrier(self, netdev: str) -> bool:
        carrier = self.sysfs / "class/net" / netdev / "carrier"
        deadline = time.monotonic() + LINK_UP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if self.read(carrier) == "1":
                    return True
            except OSError:
                pass
            time.sleep(0.1)
        return False

    def verify_identity_and_bar(self, vf_path: Path) -> None:
        config = self.config
        if not config.vf_bdf or not (vf_path / "vendor").exists() or not (vf_path / "device").exists():
            self.fail("vf-not-enumerated", bdf=config.vf_bdf)
            return
        vendor = self.read(vf_path / "vendor")
        device = self.read(vf_path / "device")
        try:
            resource_line = (vf_path / "resource").read_text().splitlines()[config.bar]
            start, end, flags = parse_bar(resource_line)
        except (OSError, IndexError, ValueError):
            self.fail("vf-bar-missing", bdf=config.vf_bdf, bar=config.bar)
        else:
            if int(start, 0) == 0 or int(end, 0) == 0:
                self.fail("vf-bar-missing", bdf=config.vf_bdf, bar=config.bar)
            else:
                self.sink.emit(
                    "MK_SECONDARY_VF_BAR",
                    index=config.bar,
                    start=start,
                    end=end,
                    flags=flags,
                )
        if vendor == config.vendor and device == config.device:
            self.sink.emit(
                "MK_SECONDARY_VF_ENUMERATED",
                instance=config.instance,
                bdf=config.vf_bdf,
                vendor=vendor,
                device=device,
            )
        else:
            self.fail(
                "vf-identity",
                bdf=config.vf_bdf,
                vendor=vendor,
                device=device,
            )

    def verify_scope(self) -> None:
        config = self.config
        devices = self.sysfs / "bus/pci/devices"
        if not (devices / config.pf_bdf).exists():
            self.sink.emit(
                "MK_SECONDARY_PF_ABSENT",
                instance=config.instance,
                bdf=config.pf_bdf,
            )
        else:
            self.fail("pf-visible", bdf=config.pf_bdf)
        count, unexpected = pci_scope(devices, config.vf_bdf)
        if count == 1 and not unexpected:
            self.sink.emit(
                "MK_SECONDARY_PCI_ISOLATED",
                instance=config.instance,
                devices=count,
                vf=config.vf_bdf,
            )
        else:
            self.fail("pci-scope", devices=count, unexpected=unexpected)

    def configure_vf(self, vf_path: Path) -> str:
        config = self.config
        driver = ""
        netdev = ""
        link_ready = False
        address_ready = False
        for _ in range(100):
            driver = self.driver_name(vf_path)
            netdev = self.netdev_name(vf_path)
            if driver == config.driver and netdev:
                link_ready = self.run_busybox(
                    "ip", "link", "set", netdev, "up"
                )
                if link_ready:
                    self.run_busybox(
                        "ip", "addr", "del", config.address, "dev", netdev
                    )
                    address_ready = self.run_busybox(
                        "ip", "addr", "add", config.address, "dev", netdev
                    )
                    if address_ready:
                        break
            time.sleep(0.1)
        if driver != config.driver:
            self.fail("vf-driver", bdf=config.vf_bdf, driver=driver)
            return ""
        if not netdev:
            self.fail("vf-netdev-missing", bdf=config.vf_bdf)
            return ""
        if not link_ready:
            self.fail("vf-link-up", netdev=netdev)
            return ""
        if not address_ready:
            self.fail("vf-address", netdev=netdev)
            return ""
        if not self.wait_for_carrier(netdev):
            self.fail("vf-carrier", netdev=netdev)
            return ""
        self.sink.emit(
            "MK_SECONDARY_VF_READY",
            instance=config.instance,
            bdf=config.vf_bdf,
            driver=driver,
            netdev=netdev,
        )
        return netdev

    def verify_datapath(self, netdev: str) -> bool:
        config = self.config
        net_path = self.sysfs / "class/net" / netdev
        mac = self.read(net_path / "address")
        link = self.read(net_path / "operstate")
        tx_before = int(self.read(net_path / "statistics/tx_packets"))
        rx_before = int(self.read(net_path / "statistics/rx_packets"))
        self.sink.emit(
            "MK_SECONDARY_VF_TRAFFIC_BEFORE",
            instance=config.instance,
            netdev=netdev,
            link=link,
            mac=mac,
            tx=tx_before,
            rx=rx_before,
        )
        datapath_ok = False
        if self.run_busybox(
            "ping",
            "-I",
            netdev,
            "-c",
            "1",
            "-W",
            "5",
            "-w",
            "10",
            config.peer,
        ):
            deadline = time.monotonic() + 2.0
            while True:
                tx_after = int(self.read(net_path / "statistics/tx_packets"))
                rx_after = int(self.read(net_path / "statistics/rx_packets"))
                if rx_after > rx_before:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if rx_after > rx_before:
                reliability = {}
                for name in RELIABILITY_COUNTERS:
                    try:
                        reliability[name] = int(
                            self.read(net_path / "statistics" / name)
                        )
                    except (OSError, ValueError):
                        reliability[name] = 0
                self.sink.emit(
                    "MK_SECONDARY_VF_DATAPATH",
                    instance=config.instance,
                    netdev=netdev,
                    peer=config.peer,
                    tx_before=tx_before,
                    tx_after=tx_after,
                    rx_before=rx_before,
                    rx_after=rx_after,
                    tx_accounted=tx_after > tx_before,
                    **reliability,
                )
                datapath_ok = True
            else:
                self.fail(
                    "vf-counters",
                    netdev=netdev,
                    tx_before=tx_before,
                    tx_after=tx_after,
                    rx_before=rx_before,
                    rx_after=rx_after,
                )
        else:
            self.fail(
                "vf-ping",
                peer=config.peer,
                netdev=netdev,
                tx_before=tx_before,
                tx_after=int(self.read(net_path / "statistics/tx_packets")),
                rx_before=rx_before,
                rx_after=int(self.read(net_path / "statistics/rx_packets")),
            )
        if not datapath_ok:
            return False
        primary_ok = False
        if config.primary_peer == "none":
            self.sink.emit(
                "MK_SECONDARY_PRIMARY_REACHABLE",
                instance=config.instance,
                netdev=netdev,
                peer="not-required",
                protocol="isolated-backend",
            )
            primary_ok = True
        elif self.run_busybox(
            "ping",
            "-I",
            netdev,
            "-c",
            "1",
            "-W",
            "5",
            "-w",
            "10",
            config.primary_peer,
        ):
            self.sink.emit(
                "MK_SECONDARY_PRIMARY_REACHABLE",
                instance=config.instance,
                netdev=netdev,
                peer=config.primary_peer,
                protocol="icmp",
            )
            primary_ok = True
        else:
            self.fail("primary-ping", peer=config.primary_peer)
        return datapath_ok and primary_ok

    def rebind_vf(self, vf_path: Path) -> str:
        config = self.config
        driver_path = self.sysfs / "bus/pci/drivers" / config.driver
        try:
            (driver_path / "unbind").write_text(f"{config.vf_bdf}\n")
        except OSError:
            self.fail("vf-rebind-unbind", bdf=config.vf_bdf)
            return ""
        for _ in range(100):
            if not self.driver_name(vf_path):
                break
            time.sleep(0.05)
        if self.driver_name(vf_path):
            self.fail("vf-rebind-still-bound", bdf=config.vf_bdf)
            return ""
        try:
            (driver_path / "bind").write_text(f"{config.vf_bdf}\n")
        except OSError:
            self.fail("vf-rebind-bind", bdf=config.vf_bdf)
            return ""
        netdev = self.configure_vf(vf_path)
        if netdev and self.verify_datapath(netdev):
            self.sink.emit(
                "MK_SECONDARY_VF_REBIND_PASS",
                instance=config.instance,
                bdf=config.vf_bdf,
                netdev=netdev,
            )
            return netdev
        return ""

    def reset_vf(self, vf_path: Path) -> None:
        reset = vf_path / "reset"
        if not reset.exists():
            self.fail("vf-reset-missing", bdf=self.config.vf_bdf)
            return
        try:
            reset.write_text("1\n")
        except OSError:
            self.fail("vf-reset", bdf=self.config.vf_bdf)
            return
        netdev = self.configure_vf(vf_path)
        if not netdev or not self.verify_datapath(netdev):
            return
        self.sink.emit(
            "MK_SECONDARY_VF_FLR_PASS",
            instance=self.config.instance,
            bdf=self.config.vf_bdf,
            netdev=netdev,
        )

    def run(self) -> None:
        config = self.config
        vf_path = self.sysfs / "bus/pci/devices" / config.vf_bdf
        self.verify_identity_and_bar(vf_path)
        self.verify_scope()
        netdev = self.configure_vf(vf_path)
        if netdev and self.verify_datapath(netdev):
            netdev = self.rebind_vf(vf_path)
        else:
            netdev = ""
        if netdev:
            self.reset_vf(vf_path)
        self.config_stress = PciConfigStress(config, self.sink, vf_path / "config")
        self.config_stress.start()
        self.sink.emit("MK_SECONDARY_ALIVE", instance=config.instance, pid=os.getpid())


def wait_for_console(path: Path, attempts: int = 5) -> MarkerSink | None:
    for _ in range(attempts):
        try:
            if stat.S_ISCHR(path.stat().st_mode):
                sink = MarkerSink(path)
                sink.open()
                return sink
        except OSError:
            pass
        time.sleep(1)
    return None


def write_fallback(message: str) -> None:
    try:
        with open("/dev/console", "w") as console:
            console.write(f"{message}\n")
    except OSError:
        pass


def main() -> int:
    sink = wait_for_console(Path("/dev/mktty0"))
    if sink is None:
        write_fallback("MK_SECONDARY_FAIL missing=console")
        return 1
    try:
        config = SecondaryConfig.from_path(Path("/proc/cmdline"))
        sink.emit("MK_SECONDARY_INIT_STARTED", instance=config.instance)
        try:
            SecondaryScenario(config, sink).run()
        except Exception as error:
            sink.emit(
                "MK_SECONDARY_FAIL",
                reason="python-exception",
                error=type(error).__name__,
            )
        while True:
            time.sleep(3600)
    finally:
        sink.close()


if __name__ == "__main__":
    raise SystemExit(main())
