"""Inspect and mutate the multikernel ELF note for rejection testing."""

from __future__ import annotations

from pathlib import Path
import struct


ELF64_HEADER_SIZE = 64
ELF64_PROGRAM_HEADER_SIZE = 56
PT_LOAD = 1
PT_NOTE = 4
CURRENT_NOTE_TYPE = 0x4D4B0002
LEGACY_NOTE_TYPE = 0x4D4B


class VmlinuxNoteError(ValueError):
    """The input is not the expected exact multikernel vmlinux."""


LegacyNoteError = VmlinuxNoteError


def _aligned(value: int, alignment: int = 4) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _read_vmlinux(
    source: Path,
) -> tuple[bytearray, int, int, tuple[tuple[int, int], ...]]:
    image = bytearray(source.read_bytes())
    if (
        len(image) < ELF64_HEADER_SIZE
        or image[:4] != b"\x7fELF"
        or image[4] != 2
        or image[5] != 1
    ):
        raise VmlinuxNoteError("expected little-endian ELF64 image")

    program_offset = struct.unpack_from("<Q", image, 32)[0]
    program_size = struct.unpack_from("<H", image, 54)[0]
    program_count = struct.unpack_from("<H", image, 56)[0]
    if program_size < ELF64_PROGRAM_HEADER_SIZE:
        raise VmlinuxNoteError("invalid ELF64 program header size")
    if program_offset + program_size * program_count > len(image):
        raise VmlinuxNoteError("program header table exceeds image")

    matches: list[tuple[int, int]] = []
    load_ranges: list[tuple[int, int]] = []
    for index in range(program_count):
        header = program_offset + index * program_size
        program_type = struct.unpack_from("<I", image, header)[0]
        if program_type == PT_LOAD:
            load_address = struct.unpack_from("<Q", image, header + 24)[0]
            file_size = struct.unpack_from("<Q", image, header + 32)[0]
            if file_size:
                load_ranges.append((load_address, file_size))
            continue
        if program_type != PT_NOTE:
            continue

        note_offset = struct.unpack_from("<Q", image, header + 8)[0]
        note_size = struct.unpack_from("<Q", image, header + 32)[0]
        note_end = note_offset + note_size
        if note_end > len(image):
            raise VmlinuxNoteError("PT_NOTE exceeds image")
        cursor = note_offset
        while cursor + 12 <= note_end:
            name_size, description_size, note_type = struct.unpack_from(
                "<III", image, cursor
            )
            record_size = 12 + _aligned(name_size) + _aligned(description_size)
            if record_size < 12 or cursor + record_size > note_end:
                raise VmlinuxNoteError("malformed ELF note")
            name_start = cursor + 12
            name = bytes(image[name_start : name_start + name_size]).rstrip(b"\0")
            if (
                name == b"Linux"
                and description_size == 8
                and note_type == CURRENT_NOTE_TYPE
            ):
                matches.append(
                    (cursor + 8, cursor + 12 + _aligned(name_size))
                )
            cursor += record_size

    if len(matches) != 1:
        raise VmlinuxNoteError(
            f"expected one current multikernel note, found {len(matches)}"
        )
    note_type_offset, entry_offset = matches[0]
    return image, note_type_offset, entry_offset, tuple(load_ranges)


def inspect_multikernel_vmlinux(
    source: Path,
) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Return the current note entry and nonempty file-backed PT_LOAD ranges."""
    image, _note_type_offset, entry_offset, load_ranges = _read_vmlinux(source)
    return struct.unpack_from("<Q", image, entry_offset)[0], load_ranges


def patch_legacy_multikernel_note(source: Path, destination: Path) -> int:
    """Patch exactly one current Linux multikernel note to the legacy type."""
    image, note_type_offset, _entry_offset, _load_ranges = _read_vmlinux(source)
    struct.pack_into("<I", image, note_type_offset, LEGACY_NOTE_TYPE)
    destination.write_bytes(image)
    return note_type_offset


def patch_multikernel_entry(
    source: Path, destination: Path, entry: int
) -> int:
    """Patch the exact current Linux multikernel note entry."""
    image, _note_type_offset, entry_offset, _load_ranges = _read_vmlinux(source)
    struct.pack_into("<Q", image, entry_offset, entry)
    destination.write_bytes(image)
    return entry_offset
