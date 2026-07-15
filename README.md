# Multikernel QEMU feasibility harness

This directory builds the local `linux/` tree from `mk-master`, creates two tiny initramfs images, and boots the result in QEMU. The primary guest creates instance 1 with a dedicated CPU and memory, loads the same kernel's ELF `vmlinux` into it, connects to its mktty console, and launches it without stopping the primary kernel.

The repository pins two upstream projects as Git submodules:

- `linux/` is the Multikernel-enabled Linux implementation used by the harness.
- `kerf/` is the official lifecycle-management tool used by the guest for baseline initialization and the complete instance lifecycle.

Clone the complete tree with:

```sh
git clone --recurse-submodules <repository-url>
cd multikernel
git submodule update --init --recursive
```

The proof is deliberately stronger than seeing a second boot log: the secondary sends `MK_SECONDARY_ALIVE` through mktty, and only after receiving it does the primary print `MK_PRIMARY_STILL_ALIVE` and `MK_DEMO_PASS simultaneous_kernels=verified`.

## Prerequisites

The harness uses GNU make, a static BusyBox, a static-capable C compiler, flex, bison, Python 3 with pip, cpio, gzip, and `qemu-system-x86_64`. `make preflight` requires these tools and fails immediately when one is unavailable; it does not substitute generated parser sources or replacement compiler tools. The host initramfs vendors a trimmed Python runtime, the pinned Kerf source, Click, rdtsc 0.2.1, and the distribution's `python3-libfdt` package. This x86 branch requires objtool, while the host lacks libelf development headers, so the first build uses `apt-get download` plus `dpkg-deb` to extract `libelf-dev` under `build/host-deps` and links it to the already-installed libelf runtime; it does not use sudo or install system packages. The preflight also requires the Linux branch to be `mk-master` unless `ALLOW_OTHER_BRANCH=1` is set.

## Usage

```sh
make build
make test
```

`make run` opens an interactive serial session. `make test` is automated and timeout-bounded; its raw output remains in `build/qemu-serial.log` whether it passes or fails.

Useful tunables:

```sh
make -j1 build JOBS=12
make test QEMU_TIMEOUT=240
```

The deterministic default topology is QEMU TCG with 2 GiB RAM and four CPUs. The primary command line reserves `512M@0x40000000`; instance 1 receives physical CPU 2 and 256 MiB. These values are intentionally fixed because Multikernel requires a contiguous pool and CPU ownership transfer.

## Targets and artifacts

- `make config`: resolves `config/multikernel-qemu.config` over `tinyconfig` and verifies required built-ins, including PCI, sparse-memory hotplug, and generic allocator APIs used unconditionally by the current Multikernel sources. Because `GENERIC_ALLOCATOR` is hidden, the fragment enables the small x86 MCE selector that provides it.
- `make kernel`: builds `build/kernel/arch/x86/boot/bzImage` and `build/kernel/vmlinux` out of tree. QEMU boots the former; Multikernel loads the latter because this branch's ELF loader supplies the physical Multikernel entry point while its bzImage loader does not.
- `make kerf-runtime`: builds the smallest self-contained Kerf/Python guest runtime used by the harness.
- `make initrd`: creates reproducible `build/host-initrd.cpio.gz` and `build/secondary-initrd.cpio.gz` archives.
- `make build`: produces every artifact.
- `make run`: boots QEMU interactively.
- `make test`: boots, checks stage-specific markers, and rejects timeouts or misleading partial output.
- `make clean`: removes only the top-level `build/` directory.

The primary init process delegates the Multikernel lifecycle to Kerf:

1. `kerf init` validates and applies the baseline resource pool.
2. `kerf create` generates and applies the instance overlay.
3. `kerf load` loads the secondary ELF kernel and initramfs.
4. `kerf exec` launches the instance.
5. `kerf kill` gracefully halts it after the simultaneous-liveness proof.

The test opens `/dev/mktty` only to automate the liveness assertion. Kerf's `console` command deliberately requires an interactive terminal, which the unattended init process does not have.

The feasibility config disables optional x86 IBT/speculation mitigations because they are unrelated to the mechanism being tested. The branch still selects objtool through x86 UACCESS validation, which is why the local libelf bootstrap is required. Do not reuse this minimal config as a production security baseline.

## Expected evidence

A passing log contains Kerf init, create, load, exec, and kill markers plus the kernel status transitions, then:

```text
MK_SECONDARY_ALIVE instance=1 ...
MK_PRIMARY_STILL_ALIVE instance=0 after=MK_SECONDARY_ALIVE
MK_DEMO_PASS simultaneous_kernels=verified
MK_QEMU_TEST_PASS ...
```

## Limitations

This validates feasibility under x86 QEMU TCG through the pinned Kerf implementation. It does not validate hardware interrupt isolation, device passthrough, networking, DAXFS, performance, or production packaging. Those should be separate real-hardware tests after this gate passes.
