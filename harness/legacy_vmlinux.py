"""Create a legacy multikernel-note vmlinux for rejection testing."""

from __future__ import annotations

from pathlib import Path
import struct


ELF64_HEADER_SIZE = 64
ELF64_PROGRAM_HEADER_SIZE = 56
PT_NOTE = 4
CURRENT_NOTE_TYPE = 0x4D4B0002
LEGACY_NOTE_TYPE = 0x4D4B


class LegacyNoteError(ValueError):
    """The input is not the expected exact multikernel vmlinux."""


def _aligned(value: int, alignment: int = 4) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def patch_legacy_multikernel_note(source: Path, destination: Path) -> int:
    """Patch exactly one current Linux multikernel note to the legacy type."""
    image = bytearray(source.read_bytes())
    if (
        len(image) < ELF64_HEADER_SIZE
        or image[:4] != b"\x7fELF"
        or image[4] != 2
        or image[5] != 1
    ):
        raise LegacyNoteError("expected little-endian ELF64 image")

    program_offset = struct.unpack_from("<Q", image, 32)[0]
    program_size = struct.unpack_from("<H", image, 54)[0]
    program_count = struct.unpack_from("<H", image, 56)[0]
    if program_size < ELF64_PROGRAM_HEADER_SIZE:
        raise LegacyNoteError("invalid ELF64 program header size")
    if program_offset + program_size * program_count > len(image):
        raise LegacyNoteError("program header table exceeds image")

    matches: list[int] = []
    for index in range(program_count):
        header = program_offset + index * program_size
        if struct.unpack_from("<I", image, header)[0] != PT_NOTE:
            continue
        note_offset = struct.unpack_from("<Q", image, header + 8)[0]
        note_size = struct.unpack_from("<Q", image, header + 32)[0]
        note_end = note_offset + note_size
        if note_end > len(image):
            raise LegacyNoteError("PT_NOTE exceeds image")
        cursor = note_offset
        while cursor + 12 <= note_end:
            name_size, description_size, note_type = struct.unpack_from(
                "<III", image, cursor
            )
            record_size = (
                12 + _aligned(name_size) + _aligned(description_size)
            )
            if record_size < 12 or cursor + record_size > note_end:
                raise LegacyNoteError("malformed ELF note")
            name_start = cursor + 12
            name = bytes(image[name_start : name_start + name_size]).rstrip(b"\0")
            if (
                name == b"Linux"
                and description_size == 8
                and note_type == CURRENT_NOTE_TYPE
            ):
                matches.append(cursor + 8)
            cursor += record_size

    if len(matches) != 1:
        raise LegacyNoteError(
            f"expected one current multikernel note, found {len(matches)}"
        )
    note_type_offset = matches[0]
    struct.pack_into("<I", image, note_type_offset, LEGACY_NOTE_TYPE)
    destination.write_bytes(image)
    return note_type_offset
