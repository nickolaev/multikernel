# Multikernel QEMU harness

This repository builds and boots concurrent Multikernel Linux instances under
QEMU. The machine contains three conventional QEMU IGB SR-IOV physical
functions on independent PCI buses, eight virtual functions in total, and
unrelated PCI leaves. The primary kernel owns the PFs, leases one VF from each
family to a distinct instance, and boots one secondary with an active VF
datapath.

The automated test passes only after it verifies that:

- all three PFs remain bound to `igb` and all eight VFs occupy singleton IOMMU
  groups;
- three VFs can be leased concurrently to distinct instances and IOMMU
  domains while the other five VFs remain host-owned;
- the active secondary sees only its assigned VF, binds `igbvf`, exchanges
  traffic with its backend, and reaches the primary PF address;
- the active secondary can be force-stopped and execute the same loaded image
  again, with the VF reset and its datapath restored before teardown;
- PF assignment, duplicate ownership, VF disable, driver rebind, reprobe, and
  surprise-unbind control-plane operations fail closed;
- deleting each instance restores the original host driver and ownership; and
- repeated lease cycles leave the primary and PF datapath operational.

## Get the source

Clone with all three pinned submodules:

```sh
git clone --recurse-submodules <repository-url> multikernel
cd multikernel
```

For an existing checkout:

```sh
git submodule update --init --recursive
```

The submodules provide:

- `linux/`: the Multikernel-enabled kernel;
- `kerf/`: lifecycle and resource-management commands used in the guest; and
- `lazy_cma/`: contiguous memory allocation for the Multikernel pool.

## Host requirements

The build expects a Debian-compatible x86-64 host with:

- GNU Make, a C compiler, flex, bison, cpio, gzip, and Python 3 with pip;
- statically linked BusyBox;
- `qemu-system-x86_64`; and
- `apt-get`, `dpkg-deb`, and `ldconfig`.

Python must be able to import Click. The build downloads `libelf-dev`,
`python3-libfdt`, and `rdtsc==0.2.1` into `build/`; it does not install system
packages or use `sudo`.

Check the checkout and host tools before a long build:

```sh
make preflight
```

By default, preflight requires the Linux submodule to be on `mk-master`. Set
`ALLOW_OTHER_BRANCH=1` only when intentionally testing another compatible
branch.

## Build and test

```sh
make build
make test
```

`make test` runs QEMU non-interactively, controls it through QMP, enforces a
timeout, and checks every required text and structured proof marker. The
complete serial log is saved as `build/qemu-serial.log` and normalized JSONL
events are saved as `build/qemu-events.jsonl` on both success and failure.

For an interactive serial console:

```sh
make run
```

Common overrides:

```sh
make build JOBS=12
make test QEMU_TIMEOUT=240
make test QEMU_CPUS=6 QEMU_MEMORY_MB=8192
```

The defaults are QEMU TCG, six CPUs, 8192 MiB of RAM, and a 600-second test
timeout. At least five CPUs and 7168 MiB are required. The primary allocates a
1024 MiB Multikernel pool. Instance 1 receives CPU 2 and 256 MiB; the two
additional lease instances reserve CPUs 3 and 4 and 256 MiB each while they are
in the ready state.

## Make targets

| Target | Result |
| --- | --- |
| `make preflight` | Validate submodules, host tools, branch, and QEMU settings. |
| `make config` | Generate and validate the minimal out-of-tree kernel config. |
| `make kernel` | Build the primary `bzImage`, secondary `vmlinux`, and modules. |
| `make kerf-runtime` | Assemble the trimmed Kerf/Python guest runtime. |
| `make lazy-cma` | Build the memory-pool module and helper. |
| `make initrd` | Build reproducible host and secondary initramfs images. |
| `make build` | Build all required artifacts. |
| `make run` | Boot QEMU with an interactive serial console. |
| `make test` | Boot QEMU and validate the complete proof sequence. |
| `make clean` | Remove only the top-level `build/` directory. |

Important artifacts:

- `build/kernel/arch/x86/boot/bzImage`: kernel booted by QEMU;
- `build/kernel/vmlinux`: ELF kernel loaded into instance 1;
- `build/host-initrd.cpio.gz`: primary initramfs;
- `build/secondary-initrd.cpio.gz`: secondary initramfs;
- `build/qemu-serial.log`: latest automated test log;
- `build/qemu-events.jsonl`: machine-readable events from the latest run; and
- `build/qemu-qmp.sock`: QMP control socket while QEMU is running.

The secondary uses `vmlinux` because this branch's ELF loader supplies the
Multikernel entry point; its bzImage loader does not.

## Successful test output

Near the end of a passing log, expect:

```text
MK_SECONDARY_VF_ENUMERATED ...
MK_SECONDARY_VF_DATAPATH ...
MK_SECONDARY_PRIMARY_REACHABLE ...
MK_SECONDARY_ALIVE ...
MK_RESTART_VF_DATAPATH_PASS instance=1 ...
MK_PRIMARY_STILL_ALIVE ...
MK_COMPLEX_CONCURRENT_LEASES_PASS leases=3 active_instances=1 ...
MK_COMPLEX_UNASSIGNED_VFS_INTACT count=5 families=3 owner=host
MK_COMPLEX_RESTORED families=3 vfs=8 ownership=host
MK_DEMO_PASS simultaneous_kernels=verified
MK_QEMU_TEST_PASS ...
```

If the test fails, search `build/qemu-serial.log` for `MK_QEMU_FAIL`,
`MK_DEMO_FAIL`, or `MK_SECONDARY_FAIL`. These markers identify the failed host,
primary, or secondary stage.

## Verification boundary

QEMU's IGB model is useful for SR-IOV functional testing but does not implement
every hardware behavior. The harness therefore makes two separate claims:

- QEMU verifies multi-PF inventory, concurrent VF lease ownership, distinct
  IOMMU-domain setup, adverse host control-plane operations, rollback,
  restoration, instance restart, and one complete VF datapath.
- Physical hardware must verify multiple simultaneously active VF datapaths,
  sustained and bidirectional DMA load, device reset behavior, interrupt
  isolation, mixed NIC models, and PCI bridge or slot removal.

The QEMU model and its stated limitations are documented in
[QEMU's IGB device documentation](https://www.qemu.org/docs/master/system/devices/igb.html).
The harness does not add kernel behavior solely to accommodate an emulated
device topology.

A passing QEMU run does not establish isolation from a malicious spawned
kernel. The spawned kernel is privileged and can bypass its normal PCI config
paths. The config wrapper is a cooperative guardrail; the host-owned IOMMU
domain is the hardware DMA boundary. QEMU also does not validate production
ownership and programming of interrupt-remapping entries, which remains part
of the host-mediated control-plane follow-up.

This remains a feasibility and regression harness for x86 QEMU TCG. It is not
a production kernel configuration and intentionally disables optional x86 IBT
and CPU-mitigation features unrelated to the test. It does not validate real
hardware performance, DAXFS, or production packaging.
