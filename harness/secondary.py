"""Secondary-kernel PCI isolation and VF datapath agent."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import time


DEFAULTS = {
    "mk_instance_id": "1",
    "mk_pf_bdf": "0000:00:02.0",
    "mk_vf_vendor": "0x8086",
    "mk_vf_device": "0x10ca",
    "mk_vf_driver": "igbvf",
    "mk_vf_address": "10.0.2.15/24",
    "mk_vf_peer": "10.0.2.2",
    "mk_primary_peer": "10.0.2.14",
}


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


class SecondaryConfig:
    def __init__(self, values: dict[str, str]):
        self.instance = int(values["mk_instance_id"])
        self.pf_bdf = values["mk_pf_bdf"]
        self.vf_bdf = values.get("mk_vf_bdf", "")
        self.vendor = values["mk_vf_vendor"]
        self.device = values["mk_vf_device"]
        self.driver = values["mk_vf_driver"]
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
        values = " ".join(f"{key}={value}" for key, value in fields.items())
        line = event if not values else f"{event} {values}"
        if self.fd is None:
            raise RuntimeError("marker sink is not open")
        os.write(self.fd, f"{line}\n".encode("ascii", errors="replace"))


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

    def fail(self, reason: str, **fields: object) -> None:
        self.sink.emit("MK_SECONDARY_FAIL", reason=reason, **fields)

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
        result = subprocess.run(
            ["/bin/busybox", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def verify_identity_and_bar(self, vf_path: Path) -> None:
        config = self.config
        if not config.vf_bdf or not (vf_path / "vendor").exists() or not (vf_path / "device").exists():
            self.fail("vf-not-enumerated", bdf=config.vf_bdf)
            return
        vendor = self.read(vf_path / "vendor")
        device = self.read(vf_path / "device")
        try:
            resource_line = (vf_path / "resource").read_text().splitlines()[0]
            start, end, flags = parse_bar(resource_line)
        except (OSError, IndexError, ValueError):
            self.fail("vf-bar0-missing", bdf=config.vf_bdf)
        else:
            if int(start, 0) == 0 or int(end, 0) == 0:
                self.fail("vf-bar0-missing", bdf=config.vf_bdf)
            else:
                self.sink.emit(
                    "MK_SECONDARY_VF_BAR",
                    index=0,
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
        driver = self.driver_name(vf_path)
        netdev = self.netdev_name(vf_path)
        if driver != config.driver:
            self.fail("vf-driver", bdf=config.vf_bdf, driver=driver)
            return ""
        if not netdev:
            self.fail("vf-netdev-missing", bdf=config.vf_bdf)
            return ""
        self.sink.emit(
            "MK_SECONDARY_VF_READY",
            instance=config.instance,
            bdf=config.vf_bdf,
            driver=driver,
            netdev=netdev,
        )
        if not self.run_busybox("ip", "link", "set", netdev, "up"):
            self.fail("vf-link-up", netdev=netdev)
        if not self.run_busybox("ip", "addr", "add", config.address, "dev", netdev):
            self.fail("vf-address", netdev=netdev)
        return netdev

    def verify_datapath(self, netdev: str) -> None:
        config = self.config
        net_path = self.sysfs / "class/net" / netdev
        mac = self.read(net_path / "address")
        link = self.read(net_path / "operstate")
        tx_before = int(self.read(net_path / "statistics/tx_packets"))
        rx_before = int(self.read(net_path / "statistics/rx_packets"))
        self.sink.emit(
            "MK_SECONDARY_VF_TRAFFIC_BEFORE",
            netdev=netdev,
            link=link,
            mac=mac,
            tx=tx_before,
            rx=rx_before,
        )
        if self.run_busybox("ping", "-c", "1", "-W", "5", config.peer):
            tx_after = int(self.read(net_path / "statistics/tx_packets"))
            rx_after = int(self.read(net_path / "statistics/rx_packets"))
            if tx_after > tx_before and rx_after > rx_before:
                self.sink.emit(
                    "MK_SECONDARY_VF_DATAPATH",
                    netdev=netdev,
                    peer=config.peer,
                    tx_before=tx_before,
                    tx_after=tx_after,
                    rx_before=rx_before,
                    rx_after=rx_after,
                )
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
            self.fail("vf-ping", peer=config.peer)
        if self.run_busybox("ping", "-c", "1", "-W", "5", config.primary_peer):
            self.sink.emit(
                "MK_SECONDARY_PRIMARY_REACHABLE",
                netdev=netdev,
                peer=config.primary_peer,
                protocol="icmp",
            )
        else:
            self.fail("primary-ping", peer=config.primary_peer)

    def run(self) -> None:
        config = self.config
        vf_path = self.sysfs / "bus/pci/devices" / config.vf_bdf
        self.verify_identity_and_bar(vf_path)
        self.verify_scope()
        netdev = self.configure_vf(vf_path)
        if netdev:
            self.verify_datapath(netdev)
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
