SHELL := /bin/bash

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
LINUX_DIR ?= $(ROOT)/linux
KERF_DIR ?= $(ROOT)/kerf
BUILD_DIR ?= $(ROOT)/build
KBUILD_DIR := $(BUILD_DIR)/kernel
HOST_DEPS := $(BUILD_DIR)/host-deps/root
KERF_RUNTIME := $(BUILD_DIR)/kerf-runtime
KERF_PYTHON_SOURCES := $(shell find '$(KERF_DIR)/src/kerf' -type f -name '*.py')
JOBS ?= $(shell nproc)
CC ?= cc
PYTHON ?= $(shell command -v python3 2>/dev/null)
BUSYBOX ?= $(shell command -v busybox 2>/dev/null)
QEMU ?= $(shell command -v qemu-system-x86_64 2>/dev/null)
LEX := $(shell command -v flex 2>/dev/null)
YACC := $(shell command -v bison 2>/dev/null)

KERNEL := $(KBUILD_DIR)/arch/x86/boot/bzImage
SECONDARY_KERNEL := $(KBUILD_DIR)/vmlinux
SECONDARY_INITRD := $(BUILD_DIR)/secondary-initrd.cpio.gz
HOST_INITRD := $(BUILD_DIR)/host-initrd.cpio.gz

.PHONY: all preflight config kernel kerf-runtime initrd build run test clean help FORCE

all: build

help:
	@printf '%s\n' \
	  'make build   - build the minimal kernel and both initramfs images' \
	  'make run     - run QEMU interactively on the serial console' \
	  'make test    - run QEMU with a timeout and assert all proof markers' \
	  'make config  - regenerate the out-of-tree minimal kernel config' \
	  'make clean   - remove only the top-level build directory'

preflight:
	@LINUX_DIR='$(LINUX_DIR)' KERF_DIR='$(KERF_DIR)' BUSYBOX='$(BUSYBOX)' \
		QEMU='$(QEMU)' CC='$(CC)' PYTHON='$(PYTHON)' \
		LEX='$(LEX)' YACC='$(YACC)' \
		'$(ROOT)/scripts/preflight.sh'

$(KBUILD_DIR)/.config: $(ROOT)/config/multikernel-qemu.config | preflight
	@mkdir -p '$(KBUILD_DIR)'
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' LEX='$(LEX)' YACC='$(YACC)' tinyconfig
	'$(LINUX_DIR)/scripts/kconfig/merge_config.sh' -m -O '$(KBUILD_DIR)' \
		'$(KBUILD_DIR)/.config' '$<'
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' LEX='$(LEX)' YACC='$(YACC)' olddefconfig
	'$(ROOT)/scripts/check-config.sh' '$(KBUILD_DIR)/.config'

config: $(KBUILD_DIR)/.config

$(HOST_DEPS)/.ready: $(ROOT)/scripts/prepare-host-deps.sh
	'$<' '$(BUILD_DIR)/host-deps'

FORCE:

$(KERNEL): $(KBUILD_DIR)/.config $(HOST_DEPS)/.ready FORCE
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' LEX='$(LEX)' YACC='$(YACC)' \
		HOSTCFLAGS='-I$(HOST_DEPS)/usr/include' \
		HOSTLDFLAGS='-L$(HOST_DEPS)/usr/lib/x86_64-linux-gnu' \
		-j'$(JOBS)' bzImage

kernel: $(KERNEL)

$(KERF_RUNTIME)/.ready: $(ROOT)/scripts/prepare-kerf-runtime.sh $(ROOT)/scripts/rdtsc-init.py $(KERF_PYTHON_SOURCES) | preflight
	'$<' '$(KERF_DIR)' '$(BUILD_DIR)' '$(PYTHON)'

kerf-runtime: $(KERF_RUNTIME)/.ready

$(SECONDARY_INITRD): $(ROOT)/initramfs/secondary-init $(ROOT)/scripts/build-initramfs.sh | preflight
	'$(ROOT)/scripts/build-initramfs.sh' secondary '$@' '$(BUSYBOX)' '$<'

$(HOST_INITRD): $(ROOT)/initramfs/host-init $(KERF_RUNTIME)/.ready $(KERNEL) $(SECONDARY_KERNEL) $(SECONDARY_INITRD) $(ROOT)/initramfs/baseline.dts $(ROOT)/scripts/build-initramfs.sh
	'$(ROOT)/scripts/build-initramfs.sh' host '$@' '$(BUSYBOX)' '$<' \
		'$(KERF_RUNTIME)' '$(SECONDARY_KERNEL)' '$(SECONDARY_INITRD)' '$(ROOT)/initramfs/baseline.dts'

initrd: $(SECONDARY_INITRD) $(HOST_INITRD)

build: preflight kernel kerf-runtime initrd
	@printf 'MK_BUILD_OK kernel=%s host_initrd=%s secondary_initrd=%s\n' \
		'$(KERNEL)' '$(HOST_INITRD)' '$(SECONDARY_INITRD)'

run: build
	QEMU='$(QEMU)' '$(ROOT)/scripts/run-qemu.sh' run

test: build
	QEMU='$(QEMU)' '$(ROOT)/scripts/run-qemu.sh' test

clean:
	rm -rf -- '$(BUILD_DIR)'
