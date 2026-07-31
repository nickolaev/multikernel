"""Event-aware multiplexing for secondary kernel consoles."""

from __future__ import annotations

import os
import select
import time
from typing import IO, Callable

from harness.events import EVENT_PREFIX, decode_event


def _marker_instance(line: str) -> int | None:
    if not line.startswith("MK_SECONDARY_ALIVE"):
        return None
    for token in line.split()[1:]:
        key, separator, value = token.partition("=")
        if key == "instance" and separator and value.isdigit():
            return int(value)
    return None


def wait_for_alive(
    consoles: dict[int, IO[bytes]],
    timeout: float,
    on_line: Callable[[int, str], None],
) -> bool:
    """Drain all consoles until each emits matching JSON and text alive records."""
    by_fd = {console.fileno(): instance for instance, console in consoles.items()}
    buffers = {instance: b"" for instance in consoles}
    structured: set[int] = set()
    markers: set[int] = set()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline and by_fd:
        remaining = deadline - time.monotonic()
        readable, _, _ = select.select(list(by_fd), [], [], min(remaining, 1))
        for fd in readable:
            instance = by_fd[fd]
            chunk = os.read(fd, 4096)
            if not chunk:
                del by_fd[fd]
                continue
            buffers[instance] += chunk
            while b"\n" in buffers[instance]:
                raw_line, buffers[instance] = buffers[instance].split(b"\n", 1)
                line = raw_line.decode(errors="replace")
                on_line(instance, line)
                if EVENT_PREFIX in line:
                    try:
                        event = decode_event(line)
                    except ValueError:
                        continue
                    if event["event"] == "MK_SECONDARY_ALIVE":
                        event_instance = event["fields"].get("instance")
                        if str(event_instance).isdigit():
                            structured.add(int(event_instance))
                marker_instance = _marker_instance(line)
                if marker_instance is not None:
                    markers.add(marker_instance)
        expected = set(consoles)
        if structured >= expected and markers >= expected:
            return True
    return False
