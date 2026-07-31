"""Declarative QEMU PCI topology for SR-IOV ownership testing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PciFamily:
    name: str
    pf_bdf: str
    vf_count: int
    pf_driver: str
    vf_driver: str
    pf_resource: str
    vf_resource_prefix: str
    compatible_pf: str
    compatible_vf: str
    subnet: str
    primary_address: str


PCI_FAMILIES = (
    PciFamily(
        name="igb0",
        pf_bdf="0000:00:02.0",
        vf_count=4,
        pf_driver="igb",
        vf_driver="igbvf",
        pf_resource="igbpf0",
        vf_resource_prefix="igbvf",
        compatible_pf="intel,igb",
        compatible_vf="intel,igbvf",
        subnet="10.0.2",
        primary_address="10.0.2.14/24",
    ),
    PciFamily(
        name="igb1",
        pf_bdf="0000:01:00.0",
        vf_count=2,
        pf_driver="igb",
        vf_driver="igbvf",
        pf_resource="igbpf1",
        vf_resource_prefix="igb1vf",
        compatible_pf="intel,igb",
        compatible_vf="intel,igbvf",
        subnet="10.0.3",
        primary_address="10.0.3.14/24",
    ),
    PciFamily(
        name="igb2",
        pf_bdf="0000:02:00.0",
        vf_count=2,
        pf_driver="igb",
        vf_driver="igbvf",
        pf_resource="igbpf2",
        vf_resource_prefix="igb2vf",
        compatible_pf="intel,igb",
        compatible_vf="intel,igbvf",
        subnet="10.0.4",
        primary_address="10.0.4.14/24",
    ),
)


def complex_pci_args() -> list[str]:
    """Return three IGB PFs on distinct buses and unrelated PCI leaves."""
    return [
        "-netdev",
        "user,id=igb0-net,net=10.0.2.0/24",
        "-device",
        "igb,id=igb0,addr=0x2,netdev=igb0-net",
        "-device",
        "pcie-root-port,id=igb1-port,addr=0x3,chassis=1,slot=3",
        "-netdev",
        "user,id=igb1-net,net=10.0.3.0/24",
        "-device",
        "igb,id=igb1,bus=igb1-port,addr=0x0,netdev=igb1-net",
        "-device",
        "pcie-root-port,id=igb2-port,addr=0x4,chassis=2,slot=4",
        "-netdev",
        "user,id=igb2-net,net=10.0.4.0/24",
        "-device",
        "igb,id=igb2,bus=igb2-port,addr=0x0,netdev=igb2-net",
        "-device",
        "virtio-rng-pci,id=noise-rng,addr=0x5",
        "-device",
        "virtio-balloon-pci,id=noise-balloon,addr=0x6",
    ]
