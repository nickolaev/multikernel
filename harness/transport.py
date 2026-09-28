"""Validation helpers for the transport-only multikernel scenario."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from harness.events import iter_events
from harness.qemu import FAILURE_MARKERS as HARNESS_FAILURE_MARKERS
from harness.qemu import KERNEL_FAILURE_MARKERS


IMMEDIATE_FAILURE_MARKERS = (
    "MK_TRANSPORT_FAIL",
    *HARNESS_FAILURE_MARKERS,
)
KERNEL_DIAGNOSTIC_MARKERS = (
    "BUG:",
    "Kernel panic",
    "KASAN:",
    "use-after-free",
    *KERNEL_FAILURE_MARKERS,
)
FAILURE_MARKERS = (
    *IMMEDIATE_FAILURE_MARKERS,
    *KERNEL_DIAGNOSTIC_MARKERS,
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TRANSPORT_SEQUENCE_MARKER = re.compile(
    r"^MK_TRANSPORT_STREAM instance=(\d+):MK_TRANSPORT_SEQUENCE "
    r"instance=(\d+) sequence=(\d+)\r*$", re.MULTILINE
)
MINIMUM_SURVIVOR_SEQUENCES = 96
RACE_CYCLES = 20


class TransportEvidenceError(RuntimeError):
    """Transport scenario evidence is incomplete or contradictory."""


def event_int(event: dict[str, object], name: str) -> int:
    fields = event.get("fields")
    if not isinstance(fields, dict):
        raise TransportEvidenceError(f"missing-fields event={event.get('event')}")
    try:
        return int(fields[name])
    except (KeyError, TypeError, ValueError) as error:
        raise TransportEvidenceError(
            f"invalid-field event={event.get('event')} field={name}"
        ) from error


@dataclass
class SequenceTracker:
    """Prove that one survivor stream is continuous and unique."""

    instance: int
    expected: int = 0

    @property
    def count(self) -> int:
        return self.expected

    def observe(self, event: dict[str, object]) -> None:
        instance = event_int(event, "instance")
        sequence = event_int(event, "sequence")
        if instance != self.instance:
            raise TransportEvidenceError(
                f"wrong-instance expected={self.instance} actual={instance}"
            )
        if sequence != self.expected:
            raise TransportEvidenceError(
                f"sequence-discontinuity instance={instance} "
                f"expected={self.expected} actual={sequence}"
            )
        self.expected += 1


def _validate_increasing(instance: int, values: Sequence[int]) -> None:
    previous = -1
    for value in values:
        if value <= previous:
            raise TransportEvidenceError(
                f"sequence-discontinuity instance={instance} "
                f"expected={previous + 1} actual={value}"
            )
        previous = value


def transport_sequence_values(
    log_text: str, events: Sequence[dict[str, object]], instance: int
) -> list[int]:
    """Merge intact JSON and text relay evidence for one sequence stream."""
    json_values = []
    for event in events:
        if event.get("event") != "MK_TRANSPORT_SEQUENCE":
            continue
        event_instance = event_int(event, "instance")
        if event_instance == instance:
            json_values.append(event_int(event, "sequence"))

    text_values = []
    for match in TRANSPORT_SEQUENCE_MARKER.finditer(log_text):
        relay_instance, marker_instance, sequence = map(int, match.groups())
        if relay_instance != marker_instance:
            raise TransportEvidenceError(
                "sequence-marker-instance-mismatch "
                f"relay={relay_instance} marker={marker_instance}"
            )
        if marker_instance == instance:
            text_values.append(sequence)

    # Each evidence channel must remain ordered and unique. A record may be
    # absent from one channel when host printk splits that serial line, so
    # merge the two channels only after validating them independently.
    _validate_increasing(instance, json_values)
    _validate_increasing(instance, text_values)
    return sorted(set(json_values) | set(text_values))


def race_start_order(cycle: int) -> tuple[str, str]:
    if cycle < 1:
        raise ValueError("cycle must be positive")
    return ("exec", "unload") if cycle % 2 else ("unload", "exec")


def _one_event(
    events: Sequence[dict[str, object]], name: str
) -> dict[str, object]:
    matches = [event for event in events if event.get("event") == name]
    if len(matches) != 1:
        raise TransportEvidenceError(f"event-count event={name} count={len(matches)}")
    return matches[0]


def validate_transport_log(log_text: str) -> None:
    """Validate exact-SHA binding and survivor continuity from serial output."""
    for marker in FAILURE_MARKERS:
        if marker in log_text:
            raise TransportEvidenceError(f"failure-marker marker={marker!r}")
    try:
        events = list(iter_events(log_text))
    except ValueError as error:
        raise TransportEvidenceError("malformed-event") from error

    evidence = _one_event(events, "MK_TRANSPORT_EVIDENCE")
    evidence_fields = evidence.get("fields")
    if not isinstance(evidence_fields, dict):
        raise TransportEvidenceError("missing-evidence-fields")
    for name in ("kernel_sha", "fixture_sha"):
        value = str(evidence_fields.get(name, ""))
        if not SHA_PATTERN.fullmatch(value):
            raise TransportEvidenceError(f"invalid-sha field={name}")
    if not SHA256_PATTERN.fullmatch(
        str(evidence_fields.get("bzimage_sha256", ""))
    ):
        raise TransportEvidenceError("invalid-sha field=bzimage_sha256")

    raw_bzimage = _one_event(events, "MK_TRANSPORT_RAW_BZIMAGE_PASS")
    raw_fields = raw_bzimage.get("fields")
    if not isinstance(raw_fields, dict):
        raise TransportEvidenceError("missing-raw-bzimage-fields")
    if (
        event_int(raw_bzimage, "instance") != 4
        or event_int(raw_bzimage, "ready") < 1
        or event_int(raw_bzimage, "sequences") < 1
        or str(raw_fields.get("loader")) != "kexec_file_load"
        or str(raw_fields.get("status")) != "active"
    ):
        raise TransportEvidenceError("invalid-raw-bzimage-evidence")
    for name in ("kernel_sha", "fixture_sha"):
        if raw_fields.get(name) != evidence_fields.get(name):
            raise TransportEvidenceError(f"raw-bzimage-sha-mismatch field={name}")
    if not SHA256_PATTERN.fullmatch(str(raw_fields.get("image_sha256", ""))):
        raise TransportEvidenceError("invalid-raw-bzimage-image-sha256")
    if raw_fields.get("image_sha256") != evidence_fields.get("bzimage_sha256"):
        raise TransportEvidenceError("raw-bzimage-image-sha256-mismatch")

    legacy_rejected = _one_event(events, "MK_TRANSPORT_LEGACY_REJECT_PASS")
    if (
        event_int(legacy_rejected, "instance") != 1
        or event_int(legacy_rejected, "attempts") != 16
        or event_int(legacy_rejected, "allocations") != 0
        or str(legacy_rejected.get("fields", {}).get("status")) != "ready"
    ):
        raise TransportEvidenceError("invalid-legacy-rejection-evidence")

    ring_passed = _one_event(events, "MK_RING_TEST_FIXTURE_PASS")
    if (
        event_int(ring_passed, "zero") != 1
        or event_int(ring_passed, "max") != 4096
        or event_int(ring_passed, "fifo") != 130
        or event_int(ring_passed, "concurrent") != 192
        or event_int(ring_passed, "full_sent") != 63
        or event_int(ring_passed, "fairness") < 128
        or event_int(ring_passed, "unregister") != 1
        or event_int(ring_passed, "peer") != 1
        or event_int(ring_passed, "oversized") != 1
    ):
        raise TransportEvidenceError("invalid-ring-test-evidence")
    if "multikernel: IPI ring full for instance" not in log_text:
        raise TransportEvidenceError("missing-ring-full-warning")
    if "multikernel: oversized IPI payload (4097 bytes) for instance 1" not in log_text:
        raise TransportEvidenceError("missing-oversized-ipi-warning")

    passed = _one_event(events, "MK_TRANSPORT_TEST_PASS")
    victim = event_int(passed, "victim")
    survivor_ids = (
        event_int(passed, "survivor_a"),
        event_int(passed, "survivor_b"),
    )
    if victim in survivor_ids or survivor_ids[0] == survivor_ids[1]:
        raise TransportEvidenceError("invalid-peer-roles")
    if event_int(passed, "relaunch_count") != 1:
        raise TransportEvidenceError("invalid-relaunch-count")
    active_unload = _one_event(events, "MK_TRANSPORT_ACTIVE_UNLOAD_REJECTED")
    post_park_reload = _one_event(events, "MK_TRANSPORT_POST_PARK_RELOAD_PASS")
    stress = _one_event(events, "MK_TRANSPORT_EXEC_UNLOAD_STRESS_PASS")
    if (
        event_int(active_unload, "victim") != victim
        or event_int(post_park_reload, "victim") != victim
    ):
        raise TransportEvidenceError("victim-lifecycle-mismatch")
    cycles = event_int(stress, "cycles")
    race_events = [
        event for event in events if event.get("event") == "MK_TRANSPORT_RACE_OUTCOME"
    ]
    if cycles != RACE_CYCLES or len(race_events) != cycles:
        raise TransportEvidenceError("invalid-race-stress-evidence")
    observed = {
        "exec_first_exec_wins": 0,
        "exec_first_unload_wins": 0,
        "unload_first_exec_wins": 0,
        "unload_first_unload_wins": 0,
    }
    for expected_cycle, event in enumerate(race_events, 1):
        cycle = event_int(event, "cycle")
        fields = event.get("fields")
        if cycle != expected_cycle or not isinstance(fields, dict):
            raise TransportEvidenceError("invalid-race-cycle")
        order = str(fields.get("start_order", ""))
        winner = str(fields.get("winner", ""))
        expected_order = (
            "exec-first" if race_start_order(cycle)[0] == "exec" else "unload-first"
        )
        if order != expected_order or winner not in ("exec", "unload"):
            raise TransportEvidenceError("invalid-race-outcome")
        observed[f"{order.replace('-', '_')}_{winner}_wins"] += 1
    for name, count in observed.items():
        if event_int(stress, name) != count:
            raise TransportEvidenceError("race-summary-mismatch")
    if event_int(stress, "exec_wins") != sum(
        count for name, count in observed.items() if name.endswith("exec_wins")
    ) or event_int(stress, "unload_wins") != sum(
        count for name, count in observed.items() if name.endswith("unload_wins")
    ):
        raise TransportEvidenceError("race-summary-mismatch")
    minimum = event_int(passed, "minimum_sequences")
    if minimum < MINIMUM_SURVIVOR_SEQUENCES:
        raise TransportEvidenceError(
            f"insufficient-pass-claim minimum={minimum}"
        )
    for suffix in ("a", "b"):
        baseline = event_int(passed, f"baseline_{suffix}")
        during = event_int(passed, f"during_{suffix}")
        final = event_int(passed, f"final_{suffix}")
        if during - baseline < 32 or final - during < 32:
            raise TransportEvidenceError(
                f"survivor-starvation survivor={suffix} "
                f"baseline={baseline} during={during} final={final}"
            )

    for instance in survivor_ids:
        tracker = SequenceTracker(instance)
        for sequence in transport_sequence_values(log_text, events, instance):
            tracker.observe({
                "fields": {"instance": instance, "sequence": sequence}
            })
        if tracker.count < minimum:
            raise TransportEvidenceError(
                f"insufficient-sequences instance={instance} "
                f"count={tracker.count} minimum={minimum}"
            )
