SHELL := /bin/bash

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
LINUX_DIR ?= $(ROOT)/linux
KERF_DIR ?= $(ROOT)/kerf
LAZY_CMA_DIR ?= $(ROOT)/lazy_cma
BUILD_DIR ?= $(ROOT)/build
KBUILD_DIR := $(BUILD_DIR)/kernel
HOST_DEPS := $(BUILD_DIR)/host-deps/root
KERF_RUNTIME := $(BUILD_DIR)/kerf-runtime
GUEST_SYSROOT := $(BUILD_DIR)/guest-sysroot
LAZY_CMA_BUILD := $(BUILD_DIR)/lazy-cma
GUEST_TOOLS := $(BUILD_DIR)/guest-tools
GUEST_BUSYBOX := $(GUEST_TOOLS)/busybox-x86_64
KERF_PYTHON_SOURCES := $(shell find '$(KERF_DIR)/src/kerf' -type f -name '*.py' 2>/dev/null)
HARNESS_PYTHON_SOURCES := $(shell find '$(ROOT)/harness' -type f -name '*.py' 2>/dev/null)
LAZY_CMA_SOURCES := $(LAZY_CMA_DIR)/lazy_cma.c $(LAZY_CMA_DIR)/lazy_cma_tool.c $(LAZY_CMA_DIR)/version.h
JOBS ?= $(shell nproc)
HOST_ARCH ?= $(shell uname -m)
GUEST_ARCH ?= x86_64
KERNEL_ARCH ?= x86
HOSTCC ?= cc
CROSS_COMPILE ?= $(if $(filter x86_64 amd64,$(HOST_ARCH)),,x86_64-linux-gnu-)
TARGET_CC ?= $(if $(CROSS_COMPILE),$(CROSS_COMPILE)gcc,cc)
PYTHON ?= $(shell command -v python3 2>/dev/null)
BUSYBOX ?= $(GUEST_BUSYBOX)
QEMU ?= $(shell command -v qemu-system-x86_64 2>/dev/null)
QEMU_CPUS ?= 12
QEMU_MEMORY_MB ?= 8192
QEMU_TIMEOUT ?= 1200
QEMU_IDLE_TIMEOUT ?= 120
LEX := $(shell command -v flex 2>/dev/null)
YACC := $(shell command -v bison 2>/dev/null)

KERNEL := $(KBUILD_DIR)/arch/x86/boot/bzImage
SECONDARY_KERNEL := $(KBUILD_DIR)/vmlinux
SECONDARY_INITRD := $(BUILD_DIR)/secondary-initrd.cpio.gz
HOST_INITRD := $(BUILD_DIR)/host-initrd.cpio.gz

.PHONY: all preflight config kernel kerf-runtime lazy-cma initrd build unit-test run test clean help FORCE

all: build

help:
	@printf '%s\n' \
	  'make preflight    - validate submodules, tools, branch, and QEMU settings' \
	  'make config       - generate and validate the minimal kernel config' \
	  'make kernel       - build the kernel and modules' \
	  'make kerf-runtime - assemble the Kerf/Python guest runtime' \
	  'make lazy-cma     - build the contiguous-memory module and helper' \
	  'make initrd       - build the host and secondary initramfs images' \
	  'make build        - build all required artifacts' \
	  'make unit-test    - run the Python harness unit tests' \
	  'make run          - run QEMU interactively on the serial console' \
	  'make test         - run QEMU and assert all proof markers' \
	  'make clean        - remove only the top-level build directory'

$(GUEST_BUSYBOX): $(ROOT)/scripts/prepare-guest-busybox.sh
	'$<' '$(GUEST_TOOLS)'

preflight: $(BUSYBOX)
	@LINUX_DIR='$(LINUX_DIR)' KERF_DIR='$(KERF_DIR)' LAZY_CMA_DIR='$(LAZY_CMA_DIR)' BUSYBOX='$(BUSYBOX)' \
		QEMU='$(QEMU)' HOST_ARCH='$(HOST_ARCH)' GUEST_ARCH='$(GUEST_ARCH)' \
		QEMU_CPUS='$(QEMU_CPUS)' QEMU_MEMORY_MB='$(QEMU_MEMORY_MB)' \
		QEMU_TIMEOUT='$(QEMU_TIMEOUT)' QEMU_IDLE_TIMEOUT='$(QEMU_IDLE_TIMEOUT)' \
		KERNEL_ARCH='$(KERNEL_ARCH)' CROSS_COMPILE='$(CROSS_COMPILE)' \
		HOSTCC='$(HOSTCC)' TARGET_CC='$(TARGET_CC)' PYTHON='$(PYTHON)' \
		LEX='$(LEX)' YACC='$(YACC)' \
		'$(ROOT)/scripts/preflight.sh'

$(KBUILD_DIR)/.config: $(ROOT)/config/multikernel-qemu.config | preflight
	@mkdir -p '$(KBUILD_DIR)'
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' ARCH='$(KERNEL_ARCH)' \
		CROSS_COMPILE='$(CROSS_COMPILE)' HOSTCC='$(HOSTCC)' LEX='$(LEX)' YACC='$(YACC)' tinyconfig
	'$(LINUX_DIR)/scripts/kconfig/merge_config.sh' -m -O '$(KBUILD_DIR)' \
		'$(KBUILD_DIR)/.config' '$<'
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' ARCH='$(KERNEL_ARCH)' \
		CROSS_COMPILE='$(CROSS_COMPILE)' HOSTCC='$(HOSTCC)' LEX='$(LEX)' YACC='$(YACC)' olddefconfig
	'$(ROOT)/scripts/check-config.sh' '$(KBUILD_DIR)/.config'

config: $(KBUILD_DIR)/.config

$(HOST_DEPS)/.ready: $(ROOT)/scripts/prepare-host-deps.sh
	'$<' '$(BUILD_DIR)/host-deps'

FORCE:

$(KERNEL): $(KBUILD_DIR)/.config $(HOST_DEPS)/.ready FORCE
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' LEX='$(LEX)' YACC='$(YACC)' \
		ARCH='$(KERNEL_ARCH)' CROSS_COMPILE='$(CROSS_COMPILE)' HOSTCC='$(HOSTCC)' \
		HOSTCFLAGS='-I$(HOST_DEPS)/usr/include' \
		HOSTLDFLAGS='-L$(HOST_DEPS)/usr/lib' \
		-j'$(JOBS)' bzImage modules

kernel: $(KERNEL)

$(GUEST_SYSROOT)/.ready: $(ROOT)/scripts/prepare-guest-sysroot.sh | preflight
	'$<' '$(GUEST_SYSROOT)'

$(KERF_RUNTIME)/.ready: $(ROOT)/scripts/prepare-kerf-runtime.sh $(ROOT)/scripts/rdtsc-init.py $(KERF_PYTHON_SOURCES) $(GUEST_SYSROOT)/.ready | preflight
	'$<' '$(KERF_DIR)' '$(BUILD_DIR)' '$(GUEST_SYSROOT)' '$(TARGET_CC)' '$(PYTHON)'

kerf-runtime: $(KERF_RUNTIME)/.ready

$(LAZY_CMA_BUILD)/.ready: $(KERNEL) $(ROOT)/config/lazy-cma.Kbuild $(LAZY_CMA_SOURCES)
	rm -rf -- '$(LAZY_CMA_BUILD)'
	mkdir -p '$(LAZY_CMA_BUILD)'
	install -m 0644 '$(ROOT)/config/lazy-cma.Kbuild' '$(LAZY_CMA_BUILD)/Makefile'
	install -m 0644 '$(LAZY_CMA_DIR)/lazy_cma.c' '$(LAZY_CMA_DIR)/version.h' '$(LAZY_CMA_BUILD)/'
	$(MAKE) -C '$(KBUILD_DIR)' M='$(LAZY_CMA_BUILD)' ARCH='$(KERNEL_ARCH)' \
		CROSS_COMPILE='$(CROSS_COMPILE)' HOSTCC='$(HOSTCC)' -j'$(JOBS)' modules
	$(TARGET_CC) -static -Wall -O2 -I'$(LAZY_CMA_DIR)' -o '$(LAZY_CMA_BUILD)/lazy_cma_tool' \
		'$(LAZY_CMA_DIR)/lazy_cma_tool.c'
	file '$(LAZY_CMA_BUILD)/lazy_cma_tool' | grep -q 'x86-64'
	touch '$@'

lazy-cma: $(LAZY_CMA_BUILD)/.ready

$(SECONDARY_INITRD): $(ROOT)/initramfs/secondary-init $(ROOT)/scripts/build-initramfs.sh \
		$(KERF_RUNTIME)/.ready $(HARNESS_PYTHON_SOURCES) | preflight
	'$(ROOT)/scripts/build-initramfs.sh' secondary '$@' '$(BUSYBOX)' '$<' \
		'$(KERF_RUNTIME)' '$(ROOT)/harness'

$(HOST_INITRD): $(ROOT)/initramfs/host-init $(KERF_RUNTIME)/.ready $(LAZY_CMA_BUILD)/.ready $(KERNEL) $(SECONDARY_KERNEL) $(SECONDARY_INITRD) $(HARNESS_PYTHON_SOURCES) $(ROOT)/scripts/build-initramfs.sh
	'$(ROOT)/scripts/build-initramfs.sh' host '$@' '$(BUSYBOX)' '$<' \
		'$(KERF_RUNTIME)' '$(SECONDARY_KERNEL)' '$(SECONDARY_INITRD)' \
		'$(LAZY_CMA_BUILD)/lazy_cma.ko' '$(LAZY_CMA_BUILD)/lazy_cma_tool' \
		'$(ROOT)/harness'

initrd: $(SECONDARY_INITRD) $(HOST_INITRD)

build: preflight kernel kerf-runtime initrd
	@printf 'MK_BUILD_OK kernel=%s host_initrd=%s secondary_initrd=%s\n' \
		'$(KERNEL)' '$(HOST_INITRD)' '$(SECONDARY_INITRD)'

unit-test:
	'$(PYTHON)' -m unittest discover -s '$(ROOT)/tests' -v

run: build
	PYTHON='$(PYTHON)' QEMU='$(QEMU)' '$(ROOT)/scripts/run-qemu.sh' run

test: unit-test build
	PYTHON='$(PYTHON)' QEMU='$(QEMU)' QEMU_CPUS='$(QEMU_CPUS)' \
		QEMU_MEMORY_MB='$(QEMU_MEMORY_MB)' QEMU_TIMEOUT='$(QEMU_TIMEOUT)' \
		QEMU_IDLE_TIMEOUT='$(QEMU_IDLE_TIMEOUT)' '$(ROOT)/scripts/run-qemu.sh' test

clean:
	rm -rf -- '$(BUILD_DIR)'
