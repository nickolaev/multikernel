"""Event-aware multiplexing for secondary kernel consoles."""

from __future__ import annotations

import os
import select
import time
from typing import IO, Callable

from harness.events import EVENT_PREFIX, decode_event


EventPredicate = Callable[[dict[str, object]], bool]


class ConsoleEventReader:
    """Read and match structured events from one console stream."""

    def __init__(self, console: IO[bytes], on_line: Callable[[str], None]):
        self.console = console
        self.on_line = on_line
        self.buffer = b""
        self.closed = False

    @staticmethod
    def _matches(
        event: dict[str, object],
        event_name: str,
        predicate: EventPredicate | None,
    ) -> bool:
        return event["event"] == event_name and (
            predicate is None or predicate(event)
        )

    def _read(self, timeout: float) -> tuple[bool, list[dict[str, object]]]:
        if self.closed:
            return False, []
        readable, _, _ = select.select([self.console.fileno()], [], [], timeout)
        if not readable:
            return False, []
        chunk = os.read(self.console.fileno(), 4096)
        if not chunk:
            self.closed = True
            return False, []
        self.buffer += chunk
        events: list[dict[str, object]] = []
        while b"\n" in self.buffer:
            raw_line, self.buffer = self.buffer.split(b"\n", 1)
            line = raw_line.decode(errors="replace")
            self.on_line(line)
            if EVENT_PREFIX not in line:
                continue
            try:
                events.append(decode_event(line))
            except ValueError:
                continue
        return True, events

    def drain_for_event(
        self,
        event_name: str,
        predicate: EventPredicate | None = None,
        *,
        idle_timeout: float = 0,
        timeout: float | None = None,
    ) -> dict[str, object] | None:
        """Drain through a quiet period, failing if its time cap expires."""
        matched = None
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.closed:
            wait = idle_timeout
            can_confirm_idle = True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("console did not become idle")
                can_confirm_idle = remaining >= idle_timeout
                wait = min(wait, remaining)
            consumed, events = self._read(wait)
            if not consumed:
                if not self.closed and not can_confirm_idle:
                    raise TimeoutError("console did not become idle")
                break
            for event in events:
                if self._matches(event, event_name, predicate):
                    matched = event
        return matched

    def wait_for_event(
        self,
        event_name: str,
        timeout: float,
        predicate: EventPredicate | None = None,
        *,
        drain: bool = False,
    ) -> dict[str, object] | None:
        """Wait for a matching event, optionally draining queued successors."""
        deadline = time.monotonic() + timeout
        while not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            _consumed, events = self._read(min(remaining, 1))
            matched = None
            for event in events:
                if self._matches(event, event_name, predicate):
                    matched = event
            if matched is not None:
                if drain:
                    return self.drain_for_event(event_name, predicate) or matched
                return matched
        return None


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
