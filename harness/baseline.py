"""Device-tree baseline generation for Multikernel test inventories."""

from __future__ import annotations

from harness.inventory import PciFunction


def render_single_vf_baseline(
    memory_base: str, pf: PciFunction, vf: PciFunction
) -> str:
    """Render the established single-IGB inventory without text substitution."""
    return f"""/dts-v1/;

/ {{
\tcompatible = \"multikernel-v1\";
\tresources {{
\t\tcpus = /bits/ 64 <2 3>;
\t\tmemory-base = <{memory_base}>;
\t\tmemory-bytes = <0x20000000>;
\t\tpci-host-bridges {{
\t\t\thost@0000,00 {{
\t\t\t\tsegment = <0>;
\t\t\t\tbus-range = <0 255>;
\t\t\t\tecam-base = /bits/ 64 <0xb0000000>;
\t\t\t}};
\t\t}};
\t\tdevices {{
\t\t\tigbpf0 {{
\t\t\t\tdevice-type = \"pci\";
\t\t\t\tcompatible = \"intel,igb\";
\t\t\t\tpci-id = \"{pf.bdf}\";
\t\t\t\tvendor-id = <0x{pf.vendor:04x}>;
\t\t\t\tdevice-id = <0x{pf.device:04x}>;
\t\t\t}};

\t\t\tigbvf0 {{
\t\t\t\tdevice-type = \"pci\";
\t\t\t\tcompatible = \"intel,igbvf\";
\t\t\t\tpci-id = \"{vf.bdf}\";
\t\t\t\tvendor-id = <0x{vf.vendor:04x}>;
\t\t\t\tdevice-id = <0x{vf.device:04x}>;
\t\t\t}};
\t\t}};
\t}};
}};
"""
