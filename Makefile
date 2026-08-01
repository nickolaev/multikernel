SHELL := /bin/bash

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
LINUX_DIR ?= $(ROOT)/linux
KERF_DIR ?= $(ROOT)/kerf
LAZY_CMA_DIR ?= $(ROOT)/lazy_cma
QEMU_DIR ?= $(ROOT)/qemu
QEMU_BUILD_DIR ?= $(QEMU_DIR)/build-multikernel
BUILD_DIR ?= $(ROOT)/build
PREBUILT_KERNEL ?=
PREBUILT_SECONDARY_KERNEL ?=
QEMU_DEPS_DIR := $(BUILD_DIR)/qemu-deps
QEMU_DEPS := $(QEMU_DEPS_DIR)/root
QEMU_NINJA := $(QEMU_DEPS)/usr/bin/ninja
QEMU_PKG_CONFIG_PATH := $(QEMU_DEPS)/usr/lib/x86_64-linux-gnu/pkgconfig:$(QEMU_DEPS)/usr/share/pkgconfig
KBUILD_DIR := $(BUILD_DIR)/kernel
HOST_DEPS := $(BUILD_DIR)/host-deps/root
KERF_RUNTIME := $(BUILD_DIR)/kerf-runtime
LAZY_CMA_BUILD := $(BUILD_DIR)/lazy-cma
KERF_PYTHON_SOURCES := $(shell find '$(KERF_DIR)/src/kerf' -type f -name '*.py' 2>/dev/null)
HARNESS_PYTHON_SOURCES := $(shell find '$(ROOT)/harness' -type f -name '*.py' 2>/dev/null)
LAZY_CMA_SOURCES := $(LAZY_CMA_DIR)/lazy_cma.c $(LAZY_CMA_DIR)/lazy_cma_tool.c $(LAZY_CMA_DIR)/version.h
JOBS ?= $(shell nproc)
CC ?= cc
PYTHON ?= $(shell command -v python3 2>/dev/null)
BUSYBOX ?= $(shell command -v busybox 2>/dev/null)
QEMU ?= $(shell command -v qemu-system-x86_64 2>/dev/null)
LEX := $(shell command -v flex 2>/dev/null)
YACC := $(shell command -v bison 2>/dev/null)

ifeq ($(strip $(PREBUILT_KERNEL)),)
KERNEL := $(KBUILD_DIR)/arch/x86/boot/bzImage
else
KERNEL := $(abspath $(PREBUILT_KERNEL))
endif
ifeq ($(strip $(PREBUILT_SECONDARY_KERNEL)),)
SECONDARY_KERNEL := $(KBUILD_DIR)/vmlinux
else
SECONDARY_KERNEL := $(abspath $(PREBUILT_SECONDARY_KERNEL))
endif
SECONDARY_INITRD := $(BUILD_DIR)/secondary-initrd.cpio.gz
HOST_INITRD := $(BUILD_DIR)/host-initrd.cpio.gz
MULTIKERNEL_QEMU := $(QEMU_BUILD_DIR)/qemu-system-x86_64

APT_TRACK ?= vf-sriov-assign
APT_BUILD_NUMBER ?= 1
APT_DEB_ARCH ?= $(shell dpkg --print-architecture)
APT_KERNEL_SHA := $(shell git -C '$(LINUX_DIR)' rev-parse --short=10 HEAD 2>/dev/null)
APT_KERNEL_FULL_SHA := $(shell git -C '$(LINUX_DIR)' rev-parse HEAD 2>/dev/null)
APT_KERNEL_BASE := $(shell $(MAKE) -s -C '$(LINUX_DIR)' kernelversion 2>/dev/null)
APT_TRACK_VERSION := $(subst -,.,$(APT_TRACK))
APT_LOCALVERSION := -999-mk-$(APT_TRACK)-g$(APT_KERNEL_SHA)
APT_KERNEL_RELEASE := $(APT_KERNEL_BASE)$(APT_LOCALVERSION)
APT_DEB_VERSION := $(subst -rc,~rc,$(APT_KERNEL_BASE))-999.$(APT_BUILD_NUMBER)+mk.$(APT_TRACK_VERSION).g$(APT_KERNEL_SHA)
APT_DEB_DIR := $(ROOT)/build/debs
APT_KBUILD_DIR := $(APT_DEB_DIR)/kernel
APT_IMAGE_DEB := $(APT_DEB_DIR)/linux-image-$(APT_KERNEL_RELEASE)_$(APT_DEB_VERSION)_$(APT_DEB_ARCH).deb
APT_SECONDARY_PACKAGE := linux-multikernel-secondary-$(APT_KERNEL_RELEASE)
APT_SECONDARY_DEB := $(APT_DEB_DIR)/$(APT_SECONDARY_PACKAGE)_$(APT_DEB_VERSION)_$(APT_DEB_ARCH).deb
APT_PACKAGE_ROOT := $(ROOT)/build/package-root
APT_PACKAGE_TEST_BUILD_DIR := $(ROOT)/build/package-test
APT_PACKAGE_KERNEL := $(APT_PACKAGE_TEST_BUILD_DIR)/kernel/arch/x86/boot/bzImage
APT_PACKAGE_SECONDARY_KERNEL := $(APT_PACKAGE_TEST_BUILD_DIR)/kernel/vmlinux

.PHONY: all preflight config kernel qemu kerf-runtime lazy-cma initrd build unit-test run test \
	deb-preflight deb-kernel deb-extract test-deb clean help FORCE

all: build

help:
	@printf '%s\n' \
	  'make preflight    - validate submodules, tools, branch, and QEMU settings' \
	  'make config       - generate and validate the minimal kernel config' \
	  'make kernel       - build the kernel and modules' \
	  'make qemu         - build the Multikernel QEMU binary' \
	  'make kerf-runtime - assemble the Kerf/Python guest runtime' \
	  'make lazy-cma     - build the contiguous-memory module and helper' \
	  'make initrd       - build the host and secondary initramfs images' \
	  'make build        - build all required artifacts' \
	  'make unit-test    - run the Python harness unit tests' \
	  'make run          - run QEMU interactively on the serial console' \
	  'make test         - run QEMU and assert all proof markers' \
	  'make deb-kernel   - build SHA-named local Linux and secondary .deb packages' \
	  'make deb-extract  - extract the local .deb packages into an isolated root' \
	  'make test-deb     - run the QEMU harness against extracted .deb payloads' \
	  'make clean        - remove only the top-level build directory'

preflight:
	@LINUX_DIR='$(LINUX_DIR)' KERF_DIR='$(KERF_DIR)' LAZY_CMA_DIR='$(LAZY_CMA_DIR)' BUSYBOX='$(BUSYBOX)' \
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

ifeq ($(strip $(PREBUILT_KERNEL)),)
$(KERNEL): $(KBUILD_DIR)/.config $(HOST_DEPS)/.ready FORCE
	$(MAKE) -C '$(LINUX_DIR)' O='$(KBUILD_DIR)' LEX='$(LEX)' YACC='$(YACC)' \
		HOSTCFLAGS='-I$(HOST_DEPS)/usr/include' \
		HOSTLDFLAGS='-L$(HOST_DEPS)/usr/lib/x86_64-linux-gnu' \
		-j'$(JOBS)' bzImage modules
endif

kernel: $(KERNEL)

$(QEMU_DEPS)/.ready: $(ROOT)/scripts/prepare-qemu-deps.sh
	'$<' '$(QEMU_DEPS_DIR)'

qemu: $(QEMU_DEPS)/.ready
	@if [[ ! -f '$(QEMU_BUILD_DIR)/build.ninja' ]]; then \
		mkdir -p '$(QEMU_BUILD_DIR)'; \
		cd '$(QEMU_BUILD_DIR)' && \
		PATH='$(QEMU_DEPS)/usr/bin:/usr/local/bin:/usr/bin:/bin' \
		PKG_CONFIG_SYSROOT_DIR='$(QEMU_DEPS)' \
		PKG_CONFIG_PATH='$(QEMU_PKG_CONFIG_PATH)' \
		../configure \
			--target-list=x86_64-softmmu --enable-multikernel --disable-werror; \
	fi
	'$(QEMU_NINJA)' -C '$(QEMU_BUILD_DIR)' qemu-system-x86_64
	test -x '$(MULTIKERNEL_QEMU)'

$(KERF_RUNTIME)/.ready: $(ROOT)/scripts/prepare-kerf-runtime.sh $(ROOT)/scripts/rdtsc-init.py $(KERF_PYTHON_SOURCES) | preflight
	'$<' '$(KERF_DIR)' '$(BUILD_DIR)' '$(PYTHON)'

kerf-runtime: $(KERF_RUNTIME)/.ready

$(LAZY_CMA_BUILD)/.ready: $(KERNEL) $(ROOT)/config/lazy-cma.Kbuild $(LAZY_CMA_SOURCES)
	rm -rf -- '$(LAZY_CMA_BUILD)'
	mkdir -p '$(LAZY_CMA_BUILD)'
	install -m 0644 '$(ROOT)/config/lazy-cma.Kbuild' '$(LAZY_CMA_BUILD)/Makefile'
	install -m 0644 '$(LAZY_CMA_DIR)/lazy_cma.c' '$(LAZY_CMA_DIR)/version.h' '$(LAZY_CMA_BUILD)/'
	$(MAKE) -C '$(KBUILD_DIR)' M='$(LAZY_CMA_BUILD)' -j'$(JOBS)' modules
	$(CC) -static -Wall -O2 -I'$(LAZY_CMA_DIR)' -o '$(LAZY_CMA_BUILD)/lazy_cma_tool' \
		'$(LAZY_CMA_DIR)/lazy_cma_tool.c'
	touch '$@'

lazy-cma: $(LAZY_CMA_BUILD)/.ready

$(SECONDARY_INITRD): $(ROOT)/initramfs/secondary-init $(ROOT)/scripts/build-initramfs.sh \
		$(KERF_RUNTIME)/.ready $(HARNESS_PYTHON_SOURCES) | preflight
	'$(ROOT)/scripts/build-initramfs.sh' secondary '$@' '$(BUSYBOX)' '$<' \
		'$(KERF_RUNTIME)' '$(ROOT)/harness'

$(HOST_INITRD): $(ROOT)/initramfs/host-init $(KERF_RUNTIME)/.ready $(LAZY_CMA_BUILD)/.ready $(KERNEL) $(SECONDARY_KERNEL) $(SECONDARY_INITRD) $(HARNESS_PYTHON_SOURCES) $(ROOT)/scripts/build-initramfs.sh qemu
	'$(ROOT)/scripts/build-initramfs.sh' host '$@' '$(BUSYBOX)' '$<' \
		'$(KERF_RUNTIME)' '$(SECONDARY_KERNEL)' '$(SECONDARY_INITRD)' \
		'$(LAZY_CMA_BUILD)/lazy_cma.ko' '$(LAZY_CMA_BUILD)/lazy_cma_tool' \
		'$(ROOT)/harness' '$(MULTIKERNEL_QEMU)'

initrd: $(SECONDARY_INITRD) $(HOST_INITRD)

build: preflight kernel kerf-runtime initrd
	@printf 'MK_BUILD_OK kernel=%s host_initrd=%s secondary_initrd=%s\n' \
		'$(KERNEL)' '$(HOST_INITRD)' '$(SECONDARY_INITRD)'

unit-test:
	'$(PYTHON)' -m unittest discover -s '$(ROOT)/tests' -v

deb-preflight:
	@ALLOW_OTHER_BRANCH=1 LINUX_DIR='$(LINUX_DIR)' KERF_DIR='$(KERF_DIR)' \
		LAZY_CMA_DIR='$(LAZY_CMA_DIR)' BUSYBOX='$(BUSYBOX)' QEMU='$(QEMU)' \
		CC='$(CC)' PYTHON='$(PYTHON)' LEX='$(LEX)' YACC='$(YACC)' \
		'$(ROOT)/scripts/preflight.sh'
	'$(ROOT)/scripts/check-deb-build-deps.sh'

ifneq ($(abspath $(KBUILD_DIR)),$(abspath $(APT_KBUILD_DIR)))
$(APT_KBUILD_DIR)/.config: $(ROOT)/config/multikernel-qemu.config | deb-preflight
	@mkdir -p '$(APT_KBUILD_DIR)'
	$(MAKE) -C '$(LINUX_DIR)' O='$(APT_KBUILD_DIR)' LEX='$(LEX)' YACC='$(YACC)' tinyconfig
	'$(LINUX_DIR)/scripts/kconfig/merge_config.sh' -m -O '$(APT_KBUILD_DIR)' \
		'$(APT_KBUILD_DIR)/.config' '$<'
	$(MAKE) -C '$(LINUX_DIR)' O='$(APT_KBUILD_DIR)' LEX='$(LEX)' YACC='$(YACC)' olddefconfig
	'$(ROOT)/scripts/check-config.sh' '$(APT_KBUILD_DIR)/.config'

endif
deb-kernel: $(APT_KBUILD_DIR)/.config
	@case '$(APT_TRACK)' in (*[!a-z0-9.+-]*|'') \
		printf 'invalid APT_TRACK: %s\n' '$(APT_TRACK)' >&2; exit 1;; esac
	DEBFULLNAME='Multikernel Build' DEBEMAIL='multikernel@localhost.invalid' \
		$(MAKE) -C '$(LINUX_DIR)' O='$(APT_KBUILD_DIR)' \
		LEX='$(LEX)' YACC='$(YACC)' LOCALVERSION='$(APT_LOCALVERSION)' \
		KDEB_PKGVERSION='$(APT_DEB_VERSION)' \
		KDEB_SOURCENAME='linux-multikernel-$(APT_TRACK)' \
		KDEB_CHANGELOG_DIST='resolute' -j'$(JOBS)' bindeb-pkg
	'$(ROOT)/scripts/build-secondary-deb.sh' '$(APT_DEB_DIR)' \
		'$(APT_KBUILD_DIR)/vmlinux' '$(APT_KERNEL_RELEASE)' '$(APT_DEB_VERSION)' \
		'$(APT_TRACK)' '$(APT_KERNEL_FULL_SHA)' '$(APT_DEB_ARCH)'
	test -f '$(APT_IMAGE_DEB)'
	test -f '$(APT_SECONDARY_DEB)'
	@printf 'MK_DEB_BUILD_OK track=%s kernel_release=%s sha=%s image=%s secondary=%s\n' \
		'$(APT_TRACK)' '$(APT_KERNEL_RELEASE)' '$(APT_KERNEL_FULL_SHA)' \
		'$(APT_IMAGE_DEB)' '$(APT_SECONDARY_DEB)'

deb-extract: deb-kernel
	'$(ROOT)/scripts/extract-local-debs.sh' '$(APT_IMAGE_DEB)' '$(APT_SECONDARY_DEB)' \
		'$(APT_PACKAGE_ROOT)' '$(APT_PACKAGE_TEST_BUILD_DIR)' '$(APT_KERNEL_RELEASE)'

test-deb: deb-extract
	$(MAKE) ALLOW_OTHER_BRANCH=1 BUILD_DIR='$(APT_PACKAGE_TEST_BUILD_DIR)' \
		KBUILD_DIR='$(APT_KBUILD_DIR)' HOST_DEPS='$(HOST_DEPS)' \
		QEMU_DEPS_DIR='$(QEMU_DEPS_DIR)' PREBUILT_KERNEL='$(APT_PACKAGE_KERNEL)' \
		PREBUILT_SECONDARY_KERNEL='$(APT_PACKAGE_SECONDARY_KERNEL)' test

run: build
	BUILD_DIR='$(BUILD_DIR)' PYTHON='$(PYTHON)' QEMU='$(QEMU)' '$(ROOT)/scripts/run-qemu.sh' run

test: unit-test build
	BUILD_DIR='$(BUILD_DIR)' PYTHON='$(PYTHON)' QEMU='$(QEMU)' '$(ROOT)/scripts/run-qemu.sh' test

clean:
	rm -rf -- '$(BUILD_DIR)'
