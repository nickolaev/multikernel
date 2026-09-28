"""Direct multikernel kexec_file_load helper for an unmodified x86 bzImage."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Callable


SYS_KEXEC_FILE_LOAD_X86_64 = 320
KEXEC_MULTIKERNEL = 0x00000010
KEXEC_MK_ID_MASK = 0x0000FFE0
KEXEC_MK_ID_SHIFT = 5
BZIMAGE_MAGIC_OFFSET = 0x202
BZIMAGE_MAGIC = b"HdrS"


class RawBzImageError(RuntimeError):
    """The raw bzImage fixture or direct syscall arguments are invalid."""


def kexec_mk_id(instance: int) -> int:
    if instance <= 0 or instance > (KEXEC_MK_ID_MASK >> KEXEC_MK_ID_SHIFT):
        raise RawBzImageError(f"invalid multikernel instance: {instance}")
    return (instance << KEXEC_MK_ID_SHIFT) & KEXEC_MK_ID_MASK


def _require_bzimage(path: Path) -> None:
    with path.open("rb") as image:
        image.seek(BZIMAGE_MAGIC_OFFSET)
        if image.read(len(BZIMAGE_MAGIC)) != BZIMAGE_MAGIC:
            raise RawBzImageError(f"not an x86 bzImage: {path}")


def load_raw_bzimage(
    kernel: Path,
    initrd: Path,
    cmdline: str,
    instance: int,
    *,
    syscall: Callable[..., int] | None = None,
) -> int:
    """Open the supplied files unchanged and load them through syscall 320."""
    _require_bzimage(kernel)
    flags = KEXEC_MULTIKERNEL | kexec_mk_id(instance)
    cmdline_bytes = cmdline.encode("utf-8")
    cmdline_buffer = ctypes.create_string_buffer(cmdline_bytes)

    kernel_fd = os.open(kernel, os.O_RDONLY)
    try:
        initrd_fd = os.open(initrd, os.O_RDONLY)
        try:
            syscall_fn = syscall
            if syscall_fn is None:
                syscall_fn = ctypes.CDLL(None, use_errno=True).syscall
                syscall_fn.argtypes = [
                    ctypes.c_long,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_ulong,
                    ctypes.c_char_p,
                    ctypes.c_ulong,
                ]
                syscall_fn.restype = ctypes.c_long
            result = syscall_fn(
                SYS_KEXEC_FILE_LOAD_X86_64,
                kernel_fd,
                initrd_fd,
                len(cmdline_bytes) + 1,
                ctypes.cast(cmdline_buffer, ctypes.c_char_p),
                flags,
            )
            if result != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        finally:
            os.close(initrd_fd)
    finally:
        os.close(kernel_fd)
    return flags
