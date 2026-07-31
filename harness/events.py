"""Human-readable and machine-readable harness event protocol."""

from __future__ import annotations

import json


EVENT_PREFIX = "MK_EVENT "


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
    if not isinstance(payload, dict):
        raise ValueError("event payload is not an object")
    if not isinstance(payload.get("event"), str):
        raise ValueError("event name is missing")
    if not isinstance(payload.get("source"), str):
        raise ValueError("event source is missing")
    if not isinstance(payload.get("fields"), dict):
        raise ValueError("event fields are missing")
    return payload


def iter_events(text: str):
    for line in text.splitlines():
        if EVENT_PREFIX in line:
            yield decode_event(line)
