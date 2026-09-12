"""Human-readable and machine-readable harness event protocol."""

from __future__ import annotations

import json
import re


EVENT_PREFIX = "MK_EVENT "
_KERNEL_CONSOLE_RECORD = re.compile(
    r"\[\s*\d+\.\d+\] [^\r\n]*(?:\r?\n|$)"
)


def _validate_event(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("event payload is not an object")
    if not isinstance(payload.get("event"), str):
        raise ValueError("event name is missing")
    if not isinstance(payload.get("source"), str):
        raise ValueError("event source is missing")
    if not isinstance(payload.get("fields"), dict):
        raise ValueError("event fields are missing")
    return payload


def format_marker(event: str, fields: dict[str, object]) -> str:
    values = " ".join(f"{key}={value}" for key, value in fields.items())
    return event if not values else f"{event} {values}"


def encode_event(event: str, fields: dict[str, object], source: str) -> str:
    payload = {"event": event, "fields": fields, "source": source}
    return EVENT_PREFIX + json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )


def encode_marker(marker: str, source: str) -> str | None:
    """Convert a key=value marker line into its structured representation."""
    tokens = marker.split()
    if not tokens or not tokens[0].startswith("MK_"):
        return None
    fields: dict[str, object] = {}
    for token in tokens[1:]:
        key, separator, value = token.partition("=")
        if separator:
            fields[key] = value
    return encode_event(tokens[0], fields, source)


def decode_event(line: str) -> dict[str, object]:
    """Decode an event even when a console label precedes it."""
    offset = line.find(EVENT_PREFIX)
    if offset < 0:
        raise ValueError("event prefix is missing")
    payload = json.loads(line[offset + len(EVENT_PREFIX) :])
    return _validate_event(payload)


def iter_events(text: str):
    """Yield valid events while tolerating timestamped console insertion.

    Each structured record is a single JSON object.  Do not silently accept a
    valid prefix followed by arbitrary bytes: doing so would turn truncated or
    concatenated records into false progress evidence.
    """
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        offset = text.find(EVENT_PREFIX, cursor)
        if offset < 0:
            return
        payload_start = offset + len(EVENT_PREFIX)
        next_offset = text.find(EVENT_PREFIX, payload_start)
        payload_end = len(text) if next_offset < 0 else next_offset
        candidate = text[payload_start:payload_end]
        candidate = _KERNEL_CONSOLE_RECORD.sub("", candidate)
        try:
            payload, end = decoder.raw_decode(candidate.lstrip())
        except json.JSONDecodeError as error:
            raise ValueError("malformed event record") from error
        trailing = candidate.lstrip()[end:]
        if trailing.strip():
            raise ValueError("trailing data in event record")
        yield _validate_event(payload)
        if next_offset < 0:
            return
        cursor = next_offset
