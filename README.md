# Multikernel QEMU harness

This repository builds and boots two concurrent Linux kernels under QEMU. The
primary kernel allocates resources, assigns a dedicated CPU, memory, and an
Intel SR-IOV virtual function to instance 1, then loads and starts the secondary
kernel without stopping itself.

The automated test passes only after it verifies that:

- the primary retains the physical NIC and stays alive;
- the secondary sees only its assigned virtual function;
- the secondary can exchange traffic through that virtual function; and
- the secondary reports liveness over `mktty` before the primary reports its
  own liveness.

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

`make test` runs QEMU non-interactively, enforces a timeout, and checks every
required proof marker. The complete serial log is saved as
`build/qemu-serial.log` on both success and failure.

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

The defaults are QEMU TCG, four CPUs, 6144 MiB of RAM, and a 300-second test
timeout. At least three CPUs and 5120 MiB are required. Instance 1 receives CPU
2 and 256 MiB from a 512 MiB pool allocated at boot.

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
- `build/secondary-initrd.cpio.gz`: secondary initramfs; and
- `build/qemu-serial.log`: latest automated test log.

The secondary uses `vmlinux` because this branch's ELF loader supplies the
Multikernel entry point; its bzImage loader does not.

## Successful test output

Near the end of a passing log, expect:

```text
MK_SECONDARY_VF_ENUMERATED ...
MK_SECONDARY_VF_DATAPATH ...
MK_SECONDARY_PRIMARY_REACHABLE ...
MK_SECONDARY_ALIVE ...
MK_PRIMARY_STILL_ALIVE ...
MK_DEMO_PASS simultaneous_kernels=verified
MK_QEMU_TEST_PASS ...
```

If the test fails, search `build/qemu-serial.log` for `MK_QEMU_FAIL`,
`MK_DEMO_FAIL`, or `MK_SECONDARY_FAIL`. These markers identify the failed host,
primary, or secondary stage.

## Scope

This is a feasibility and regression harness for x86 QEMU TCG. It is not a
production kernel configuration and intentionally disables optional x86 IBT
and CPU-mitigation features unrelated to the test. It does not validate real
hardware interrupt isolation, performance, DAXFS, or production packaging.
