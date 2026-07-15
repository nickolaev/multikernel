SHELL := /bin/bash

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
LINUX_DIR ?= $(ROOT)/linux
BUILD_DIR ?= $(ROOT)/build
KBUILD_DIR := $(BUILD_DIR)/kernel
UAPI_DIR := $(BUILD_DIR)/uapi
HOST_DEPS := $(BUILD_DIR)/host-deps/root
JOBS ?= $(shell nproc)
CC ?= cc
BUSYBOX ?= $(shell command -v busybox 2>/dev/null)
DTC ?= $(shell command -v dtc 2>/dev/null)
QEMU ?= $(shell command -v qemu-system-x86_64 2>/dev/null)
LEX := $(shell command -v flex 2>/dev/null)
YACC := $(shell command -v bison 2>/dev/null)

KERNEL := $(KBUILD_DIR)/arch/x86/boot/bzImage
SECONDARY_KERNEL := $(KBUILD_DIR)/vmlinux
MKCTL := $(BUILD_DIR)/bin/mkctl
BASELINE_DTB := $(BUILD_DIR)/baseline.dtb
INSTANCE_DTBO := $(BUILD_DIR)/instance.dtbo
SECONDARY_INITRD := $(BUILD_DIR)/secondary-initrd.cpio.gz
HOST_INITRD := $(BUILD_DIR)/host-initrd.cpio.gz

.PHONY: all preflight config kernel helper dtb initrd build run test clean help

all: build

help:
	@printf '%s\n' \
	  'make build   - build the minimal kernel and both initramfs images' \
	  'make run     - run QEMU interactively on the serial console' \
	  'make test    - run QEMU with a timeout and assert all proof markers' \
	  'make config  - regenerate the out-of-tree minimal kernel config' \
	  'make clean   - remove only the top-level build directory'

preflight:
	@LINUX_DIR='$(LINUX_DIR)' BUSYBOX='$(BUSYBOX)' DTC='$(DTC)' QEMU='$(QEMU)' CC='$(CC)' \
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

$(KERNEL): $(KBUILD_DIR)/.config $(HOST_DEPS)/.ready
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' LEX='$(LEX)' YACC='$(YACC)' \
		HOSTCFLAGS='-I$(HOST_DEPS)/usr/include' \
		HOSTLDFLAGS='-L$(HOST_DEPS)/usr/lib/x86_64-linux-gnu' \
		-j'$(JOBS)' bzImage

kernel: $(KERNEL)

$(UAPI_DIR)/.installed: | preflight
	@mkdir -p '$(UAPI_DIR)'
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' \
		INSTALL_HDR_PATH='$(UAPI_DIR)' headers_install
	@touch '$@'

$(MKCTL): $(ROOT)/tools/mkctl.c $(UAPI_DIR)/.installed | preflight
	@mkdir -p '$(@D)'
	$(CC) -std=c11 -O2 -static -Wall -Wextra -Werror \
		-I'$(UAPI_DIR)/include' '$<' -o '$@'

helper: $(MKCTL)

$(BASELINE_DTB): $(ROOT)/initramfs/baseline.dts | preflight
	@mkdir -p '$(@D)'
	$(DTC) -I dts -O dtb -o '$@' '$<'

$(INSTANCE_DTBO): $(ROOT)/initramfs/instance.dts | preflight
	@mkdir -p '$(@D)'
	$(DTC) -I dts -O dtb -o '$@' '$<'

dtb: $(BASELINE_DTB) $(INSTANCE_DTBO)

$(SECONDARY_INITRD): $(ROOT)/initramfs/secondary-init $(ROOT)/scripts/build-initramfs.sh | preflight
	'$(ROOT)/scripts/build-initramfs.sh' secondary '$@' '$(BUSYBOX)' '$<'

$(HOST_INITRD): $(ROOT)/initramfs/host-init $(MKCTL) $(KERNEL) $(SECONDARY_KERNEL) $(SECONDARY_INITRD) $(BASELINE_DTB) $(INSTANCE_DTBO) $(ROOT)/scripts/build-initramfs.sh
	'$(ROOT)/scripts/build-initramfs.sh' host '$@' '$(BUSYBOX)' '$<' \
		'$(MKCTL)' '$(SECONDARY_KERNEL)' '$(SECONDARY_INITRD)' '$(BASELINE_DTB)' '$(INSTANCE_DTBO)'

initrd: $(SECONDARY_INITRD) $(HOST_INITRD)

build: preflight kernel helper dtb initrd
	@printf 'MK_BUILD_OK kernel=%s host_initrd=%s secondary_initrd=%s\n' \
		'$(KERNEL)' '$(HOST_INITRD)' '$(SECONDARY_INITRD)'

run: build
	QEMU='$(QEMU)' '$(ROOT)/scripts/run-qemu.sh' run

test: build
	QEMU='$(QEMU)' '$(ROOT)/scripts/run-qemu.sh' test

clean:
	rm -rf -- '$(BUILD_DIR)'
