SHELL := /bin/bash

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PLATFORM ?= x86
SUPPORTED_PLATFORMS := x86 riscv

ifeq ($(filter $(PLATFORM),$(SUPPORTED_PLATFORMS)),)
$(error unsupported PLATFORM '$(PLATFORM)'; expected one of: $(SUPPORTED_PLATFORMS))
endif

LINUX_DIR ?= $(ROOT)/linux
KERF_DIR ?= $(ROOT)/kerf
BUILD_DIR ?= $(ROOT)/build/$(PLATFORM)
KBUILD_DIR := $(BUILD_DIR)/kernel
HOST_DEPS := $(BUILD_DIR)/host-deps/root
KERF_RUNTIME := $(BUILD_DIR)/kerf-runtime
SECONDARY_INITRD := $(BUILD_DIR)/secondary-initrd.cpio.gz
HOST_INITRD := $(BUILD_DIR)/host-initrd.cpio.gz

JOBS ?= $(shell nproc)
CC ?= cc
PYTHON ?= $(shell command -v python3 2>/dev/null)
BUSYBOX ?= $(shell command -v busybox 2>/dev/null)
LEX := $(shell command -v flex 2>/dev/null)
YACC := $(shell command -v bison 2>/dev/null)
HOST_LIBDIR ?= /usr/lib/x86_64-linux-gnu

ifeq ($(PLATFORM),x86)
KARCH := x86
KCONFIG_FRAGMENT := $(ROOT)/config/multikernel-qemu.config
KERNEL := $(KBUILD_DIR)/arch/x86/boot/bzImage
SECONDARY_KERNEL := $(KBUILD_DIR)/vmlinux
KERNEL_TARGET := bzImage
KBUILD_PLATFORM_FLAGS := ARCH=$(KARCH)
QEMU ?= $(shell command -v qemu-system-x86_64 2>/dev/null)
QEMU_MACHINE := q35,accel=tcg
QEMU_CPU := max
QEMU_APPEND := console=ttyS0,115200 rdinit=/init panic=-1 mkkernel_pool=512M@0x40000000 kho=on
GUEST_BUSYBOX := $(BUSYBOX)
HOST_INIT := $(ROOT)/initramfs/host-init
SECONDARY_INIT := $(ROOT)/initramfs/secondary-init
BASELINE_DTS := $(ROOT)/initramfs/baseline.dts
else
KARCH := riscv
CROSS_COMPILE ?= riscv64-linux-gnu-
KCONFIG_FRAGMENT := $(ROOT)/config/multikernel-qemu-riscv.config
KERNEL := $(KBUILD_DIR)/arch/riscv/boot/Image
SECONDARY_KERNEL := $(KBUILD_DIR)/vmlinux
KERNEL_TARGET := Image
KBUILD_PLATFORM_FLAGS := ARCH=$(KARCH) CROSS_COMPILE=$(CROSS_COMPILE)
QEMU ?= $(shell command -v qemu-system-riscv64 2>/dev/null)
QEMU_MACHINE := virt,accel=tcg
QEMU_CPU := max
QEMU_APPEND := console=ttyS0 rdinit=/init panic=-1 mkkernel_pool=512M@0xc0000000 kho=on
GUEST_BUSYBOX := $(KERF_RUNTIME)/usr/bin/busybox
HOST_INIT := $(ROOT)/initramfs/host-init-riscv
SECONDARY_INIT := $(ROOT)/initramfs/secondary-init-riscv
BASELINE_DTS := $(ROOT)/initramfs/baseline-riscv.dts
endif

.PHONY: all preflight config kernel kerf-runtime initrd build run test show-platform clean help FORCE

all: build

help:
	@printf '%s\n' \
	  'make PLATFORM=x86 build    - build the x86-64 QEMU harness' \
	  'make PLATFORM=riscv build  - build the riscv64 QEMU virt harness' \
	  'make PLATFORM=<p> run      - run QEMU interactively on the serial console' \
	  'make PLATFORM=<p> test     - run QEMU with a timeout and assert proof markers' \
	  'make PLATFORM=<p> config   - regenerate the out-of-tree kernel config' \
	  'make PLATFORM=<p> show-platform - print resolved platform settings' \
	  'make clean                 - remove only the top-level build directory'

show-platform:
	@printf 'PLATFORM=%s ARCH=%s KERNEL=%s SECONDARY_KERNEL=%s QEMU=%s MACHINE=%s\n' \
		'$(PLATFORM)' '$(KARCH)' '$(KERNEL)' '$(SECONDARY_KERNEL)' '$(QEMU)' '$(QEMU_MACHINE)'

preflight:
	@PLATFORM='$(PLATFORM)' KARCH='$(KARCH)' KERNEL_TARGET='$(KERNEL_TARGET)' \
		LINUX_DIR='$(LINUX_DIR)' KERF_DIR='$(KERF_DIR)' BUSYBOX='$(BUSYBOX)' \
		QEMU='$(QEMU)' CC='$(CC)' PYTHON='$(PYTHON)' LEX='$(LEX)' YACC='$(YACC)' \
		CROSS_COMPILE='$(CROSS_COMPILE)' \
		'$(ROOT)/scripts/preflight.sh'

$(KBUILD_DIR)/.config: $(KCONFIG_FRAGMENT) | preflight
	@mkdir -p '$(KBUILD_DIR)'
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' $(KBUILD_PLATFORM_FLAGS) \
		LEX='$(LEX)' YACC='$(YACC)' tinyconfig
	'$(LINUX_DIR)/scripts/kconfig/merge_config.sh' -m -O '$(KBUILD_DIR)' \
		'$(KBUILD_DIR)/.config' '$<'
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' $(KBUILD_PLATFORM_FLAGS) \
		LEX='$(LEX)' YACC='$(YACC)' olddefconfig
	PLATFORM='$(PLATFORM)' '$(ROOT)/scripts/check-config.sh' '$(KBUILD_DIR)/.config'

config: $(KBUILD_DIR)/.config

$(HOST_DEPS)/.ready: $(ROOT)/scripts/prepare-host-deps.sh
	'$<' '$(BUILD_DIR)/host-deps'

FORCE:

$(KERNEL): $(KBUILD_DIR)/.config $(HOST_DEPS)/.ready FORCE
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' $(KBUILD_PLATFORM_FLAGS) \
		LEX='$(LEX)' YACC='$(YACC)' \
		HOSTCFLAGS='-I$(HOST_DEPS)/usr/include' \
		HOSTLDFLAGS='-L$(HOST_LIBDIR)' \
		-j'$(JOBS)' '$(KERNEL_TARGET)'

kernel: $(KERNEL)

$(KERF_RUNTIME)/.ready: $(ROOT)/scripts/prepare-kerf-runtime.sh | preflight
	PLATFORM='$(PLATFORM)' '$<' '$(KERF_DIR)' '$(BUILD_DIR)' '$(PYTHON)'

kerf-runtime: $(KERF_RUNTIME)/.ready

$(SECONDARY_INITRD): $(SECONDARY_INIT) $(ROOT)/scripts/build-initramfs.sh $(KERF_RUNTIME)/.ready | preflight
	'$(ROOT)/scripts/build-initramfs.sh' secondary '$@' '$(GUEST_BUSYBOX)' '$<'

$(HOST_INITRD): $(HOST_INIT) $(KERF_RUNTIME)/.ready $(KERNEL) $(SECONDARY_KERNEL) $(SECONDARY_INITRD) $(BASELINE_DTS) $(ROOT)/scripts/build-initramfs.sh
	PLATFORM='$(PLATFORM)' '$(ROOT)/scripts/build-initramfs.sh' host '$@' '$(GUEST_BUSYBOX)' '$<' \
		'$(KERF_RUNTIME)' '$(SECONDARY_KERNEL)' '$(SECONDARY_INITRD)' '$(BASELINE_DTS)'

initrd: $(SECONDARY_INITRD) $(HOST_INITRD)

build: preflight kernel kerf-runtime initrd
	@printf 'MK_BUILD_OK platform=%s kernel=%s host_initrd=%s secondary_initrd=%s\n' \
		'$(PLATFORM)' '$(KERNEL)' '$(HOST_INITRD)' '$(SECONDARY_INITRD)'

run: build
	PLATFORM='$(PLATFORM)' BUILD_DIR='$(BUILD_DIR)' KERNEL='$(KERNEL)' QEMU='$(QEMU)' \
		QEMU_MACHINE='$(QEMU_MACHINE)' QEMU_CPU='$(QEMU_CPU)' QEMU_APPEND='$(QEMU_APPEND)' \
		'$(ROOT)/scripts/run-qemu.sh' run

test: build
	PLATFORM='$(PLATFORM)' BUILD_DIR='$(BUILD_DIR)' KERNEL='$(KERNEL)' QEMU='$(QEMU)' \
		QEMU_MACHINE='$(QEMU_MACHINE)' QEMU_CPU='$(QEMU_CPU)' QEMU_APPEND='$(QEMU_APPEND)' \
		'$(ROOT)/scripts/run-qemu.sh' test

clean:
	rm -rf -- '$(ROOT)/build'
