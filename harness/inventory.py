"""Typed access to the primary kernel's PCI sysfs inventory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def _hex_value(path: Path) -> int:
    return int(path.read_text().strip(), 16)


@dataclass(frozen=True)
class PciFunction:
    """A PCI function whose immutable identity comes from sysfs."""

    path: Path
    bdf: str
    vendor: int
    device: int

    @classmethod
    def from_path(cls, path: Path) -> "PciFunction":
        return cls(
            path=path,
            bdf=path.name,
            vendor=_hex_value(path / "vendor"),
            device=_hex_value(path / "device"),
        )

    @property
    def driver(self) -> str:
        link = self.path / "driver"
        try:
            return link.resolve(strict=True).name
        except FileNotFoundError:
            return ""

    @property
    def iommu_group_path(self) -> Path | None:
        link = self.path / "iommu_group"
        try:
            return link.resolve(strict=True)
        except FileNotFoundError:
            return None

    @property
    def iommu_group(self) -> str:
        path = self.iommu_group_path
        return path.name if path else ""

    def iommu_group_members(self) -> tuple[str, ...]:
        path = self.iommu_group_path
        if path is None:
            return ()
        return tuple(sorted(member.name for member in (path / "devices").iterdir()))

    def virtual_function(self, index: int) -> "PciFunction | None":
        link = self.path / f"virtfn{index}"
        try:
            path = link.resolve(strict=True)
        except FileNotFoundError:
            return None
        if not (path / "vendor").exists():
            return None
        return PciFunction.from_path(path)
