"""Device-tree baseline generation for Multikernel test inventories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from harness.inventory import PciFunction


@dataclass(frozen=True)
class PciResource:
    name: str
    compatible: str
    function: PciFunction


def _render_resource(resource: PciResource) -> str:
    function = resource.function
    return f"""			{resource.name} {{
				device-type = "pci";
				compatible = "{resource.compatible}";
				pci-id = "{function.bdf}";
				vendor-id = <0x{function.vendor:04x}>;
				device-id = <0x{function.device:04x}>;
			}};"""


def render_baseline(
    _memory_base: str,
    cpus: Sequence[int],
    memory_bytes: int,
    resources: Sequence[PciResource],
) -> str:
    """Render a baseline from discovered CPU, memory, and PCI resources."""
    cpu_cells = " ".join(str(cpu) for cpu in cpus)
    devices = "".join(_render_resource(resource) for resource in resources)
    return f"""/dts-v1/;
/ {{
	compatible = "multikernel-v1";
	resources {{
		cpus = /bits/ 64 <{cpu_cells}>;
		memory@0 {{
			size = <0x0 0x{memory_bytes:x}>;
			numa-node-id = <0>;
		}};
		devices {{{devices}
		}};
	}};
}};
"""


def render_single_vf_baseline(
    memory_base: str, pf: PciFunction, vf: PciFunction
) -> str:
    return render_baseline(
        memory_base,
        cpus=(2, 3),
        memory_bytes=0x20000000,
        resources=(
            PciResource("igbpf0", "intel,igb", pf),
            PciResource("igbvf0", "intel,igbvf", vf),
        ),
    )
