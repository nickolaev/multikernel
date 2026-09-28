"""Three-child transport isolation and restart scenario."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Mapping, Sequence

from harness.baseline import render_baseline
from harness.events import EVENT_PREFIX, decode_event
from harness.legacy_vmlinux import (
    inspect_multikernel_vmlinux,
    patch_legacy_multikernel_note,
    patch_multikernel_entry,
)
from harness.raw_bzimage import load_raw_bzimage
from harness.primary import (
    BUSYBOX,
    INSTANCES,
    ScenarioFailure,
    command,
    dmesg,
    emit,
    kerf,
)
from harness.transport import (
    IMMEDIATE_FAILURE_MARKERS,
    KERNEL_DIAGNOSTIC_MARKERS,
    MINIMUM_SURVIVOR_SEQUENCES,
    RACE_CYCLES,
    SequenceTracker,
    TransportEvidenceError,
    race_start_order,
)


CHILDREN = (("transport-a", 1, 2), ("transport-b", 2, 3), ("transport-victim", 3, 4))
RAW_CHILD = ("transport-raw-bzimage", 4, 5)
ALL_CHILDREN = (*CHILDREN, RAW_CHILD)
SURVIVORS = (1, 2)
VICTIM = 3
PHASE_SEQUENCES = 32
RACE_COMMAND_TIMEOUT = 30
RACE_REAP_TIMEOUT = 5
RACE_GATE_PROGRAM = """
import os
import sys

ready_fd = int(sys.argv[1])
start_fd = int(sys.argv[2])
os.write(ready_fd, b"1")
token = os.read(start_fd, 1)
os.close(ready_fd)
os.close(start_fd)
if token != b"1":
    raise SystemExit(125)
os.execv(sys.argv[3], sys.argv[3:])
"""


def _cmdline_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for argument in text.split():
        key, separator, value = argument.partition("=")
        if separator and key.startswith("mk_transport_"):
            values[key] = value
    return values


def _relay(instance: int, line: str) -> None:
    os.write(
        1,
        f"MK_TRANSPORT_STREAM instance={instance}:{line}\n".encode(
            "ascii", errors="replace"
        ),
    )


class ConsoleMonitor:
    """Drain one mktty endpoint and track its numbered stream."""

    def __init__(self, instance: int, console: IO[bytes], track_sequence: bool):
        self.instance = instance
        self.console = console
        self.tracker = SequenceTracker(instance) if track_sequence else None
        self.ready_count = 0
        self.ring_full_warning_count = 0
        self.error: Exception | None = None
        self.buffer = b""
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    @property
    def sequence_count(self) -> int:
        return self.tracker.count if self.tracker is not None else 0

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=5)

    def wait_for(self, predicate, timeout: float, stage: str) -> None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while not predicate():
                if self.error is not None:
                    raise ScenarioFailure(stage) from self.error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScenarioFailure(stage)
                self.condition.wait(min(remaining, 0.5))

    def _run(self) -> None:
        try:
            while not self.stop.is_set():
                readable, _, _ = select.select([self.console.fileno()], [], [], 0.2)
                if not readable:
                    continue
                chunk = os.read(self.console.fileno(), 4096)
                if not chunk:
                    time.sleep(0.01)
                    continue
                self.buffer += chunk
                while b"\n" in self.buffer:
                    raw_line, self.buffer = self.buffer.split(b"\n", 1)
                    line = raw_line.decode(errors="replace")
                    _relay(self.instance, line)
                    failure_marker = next(
                        (
                            marker
                            for marker in IMMEDIATE_FAILURE_MARKERS
                            if marker in line
                        ),
                        None,
                    )
                    if failure_marker is not None:
                        raise TransportEvidenceError(
                            f"failure-marker marker={failure_marker!r}"
                        )
                    if "multikernel: IPI ring full for instance" in line:
                        with self.condition:
                            self.ring_full_warning_count += 1
                            self.condition.notify_all()
                    if EVENT_PREFIX not in line:
                        continue
                    try:
                        event = decode_event(line)
                    except ValueError:
                        continue
                    with self.condition:
                        if event.get("event") == "MK_TRANSPORT_READY":
                            self.ready_count += 1
                        elif (
                            event.get("event") == "MK_TRANSPORT_SEQUENCE"
                            and self.tracker is not None
                        ):
                            self.tracker.observe(event)
                        self.condition.notify_all()
        except (OSError, TransportEvidenceError) as error:
            with self.condition:
                self.error = error
                self.condition.notify_all()


def _open_console(instance: int) -> IO[bytes]:
    console = open("/dev/mktty", "r+b", buffering=0)
    console.write(f"{instance}\n".encode("ascii"))
    return console


def _expect_status(name: str, expected: str) -> None:
    path = INSTANCES / name / "status"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if path.read_text().strip() == expected:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise ScenarioFailure(f"status-{name}-{expected}")


def _load_ring_test_host() -> None:
    command(
        [
            BUSYBOX,
            "insmod",
            "/lib/modules/mk_ring_test.ko",
            "role=host",
            "target_id=1",
        ],
        "ring-test-host-module",
    )


def _wait_ring_test_host(timeout: float = 90) -> int:
    result_path = Path("/sys/module/mk_ring_test/parameters/result")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = int(result_path.read_text().strip())
        if value == 1:
            break
        if value < 0:
            raise ScenarioFailure(f"ring-test-host-result-{value}")
        time.sleep(0.05)
    else:
        raise ScenarioFailure("ring-test-host-timeout")

    markers = (
        "MK_RING_TEST_FAIR_PEER_REQUEST",
        "MK_RING_TEST_FAIR_PEER_PASS",
        "MK_RING_TEST_FAIR_UNREGISTER_PASS",
    )
    while time.monotonic() < deadline:
        log = dmesg()
        positions = [log.find(marker) for marker in markers]
        match = re.search(r"MK_RING_TEST_PASS .* fairness=(\d+) ", log)
        if (
            all(position >= 0 for position in positions)
            and positions == sorted(positions)
            and "multikernel: oversized IPI payload (4097 bytes) for instance 1"
            in log
            and match
        ):
            return int(match.group(1))
        time.sleep(0.05)
    raise ScenarioFailure("ring-test-host-evidence-timeout")


def _allocate_pool() -> str:
    command([BUSYBOX, "insmod", "/lib/modules/lazy_cma.ko"], "lazy-cma-module")
    result = command(
        ["/bin/lazy_cma_tool", "-a", "1024", "-n", "Multikernel Memory Pool"],
        "pool-allocation",
        capture=True,
    )
    base = result.stdout.split()[-1]
    if not base.startswith("0x"):
        raise ScenarioFailure("pool-address")
    return base


def _assert_malformed_vmlinux_rejected(name: str, instance: int) -> None:
    attempts = 16
    source = Path("/payload/vmlinux")
    valid_entry, load_ranges = inspect_multikernel_vmlinux(source)
    if not load_ranges or not any(
        valid_entry >= base and valid_entry - base < size
        for base, size in load_ranges
    ):
        raise ScenarioFailure("valid-vmlinux-entry-outside-loads")

    lowest_load = min(base for base, _size in load_ranges)
    if lowest_load == 0:
        raise ScenarioFailure("vmlinux-has-no-below-load-address")
    ordered_loads = sorted(load_ranges)
    interior_gaps = tuple(
        base + size
        for (base, size), (next_base, _next_size) in zip(
            ordered_loads, ordered_loads[1:]
        )
        if base + size < next_base
    )
    if not interior_gaps:
        raise ScenarioFailure("vmlinux-has-no-interior-load-gap")
    invalid_entries = (
        ("zero", 0),
        ("below-load", lowest_load - 1),
        ("interior-gap", interior_gaps[0]),
        ("outside-load", max(base + size for base, size in load_ranges)),
    )

    entry_offset = -1
    kernel = Path("/run/vmlinux-rejected-note")
    for case, entry in invalid_entries:
        entry_offset = patch_multikernel_entry(source, kernel, entry)
        for attempt in range(1, attempts + 1):
            _assert_vmlinux_load_rejected(
                name,
                instance,
                kernel,
                case,
                attempt,
                "Exec format error",
            )
        emit(
            f"MK_TRANSPORT_ENTRY_REJECT_CASE_PASS instance={instance} "
            f"case={case} attempts={attempts} "
            f"pool_segment_allocations=0 status=ready"
        )

    note_offset = patch_legacy_multikernel_note(source, kernel)
    for attempt in range(1, attempts + 1):
        _assert_vmlinux_load_rejected(
            name,
            instance,
            kernel,
            "legacy",
            attempt,
            "Protocol not supported",
        )

    emit(
        f"MK_TRANSPORT_ENTRY_REJECT_PASS instance={instance} "
        f"entry_offset={entry_offset} valid_entry={valid_entry} "
        f"cases={len(invalid_entries)} "
        f"attempts={attempts * len(invalid_entries)} "
        f"pool_segment_allocations=0 status=ready"
    )
    emit(
        f"MK_TRANSPORT_LEGACY_REJECT_PASS instance={instance} "
        f"note_offset={note_offset} attempts={attempts} "
        f"pool_segment_allocations=0 status=ready"
    )


def _assert_vmlinux_load_rejected(
    name: str,
    instance: int,
    kernel: Path,
    case: str,
    attempt: int,
    expected_error: str,
) -> None:
    dmesg_before = dmesg()
    result = kerf(
        "load",
        name,
        f"--kernel={kernel}",
        "--initrd=/payload/secondary-initrd.cpio.gz",
        f"--cmdline=rdinit=/init quiet loglevel=6 panic=-1 "
        f"mk_transport_test=1 mk_instance_id={instance}",
        "--console=mktty0",
        stage=f"{case}-vmlinux-load-{attempt}",
        check=False,
        capture=True,
    )
    if result.returncode == 0 or expected_error not in result.stdout:
        raise ScenarioFailure(f"{case}-vmlinux-not-rejected-{attempt}")
    dmesg_after = dmesg()
    dmesg_delta = (
        dmesg_after[len(dmesg_before) :]
        if dmesg_after.startswith(dmesg_before)
        else dmesg_after
    )
    if re.search(
        r"kexec_file: Allocated \d+ bytes from multikernel pool",
        dmesg_delta,
    ):
        raise ScenarioFailure(f"{case}-vmlinux-allocated-segment-{attempt}")
    _expect_status(name, "ready")


def _load_child(name: str, instance: int) -> None:
    kerf(
        "load",
        name,
        "--kernel=/payload/vmlinux",
        "--initrd=/payload/secondary-initrd.cpio.gz",
        f"--cmdline=rdinit=/init quiet loglevel=6 panic=-1 "
        f"mk_transport_test=1 mk_instance_id={instance}",
        "--console=mktty0",
        stage=f"transport-load-{instance}",
    )
    _expect_status(name, "loaded")


def _run_raw_bzimage_smoke(
    kernel_sha: str,
    fixture_sha: str,
    expected_bzimage_sha256: str,
    consoles: dict[int, IO[bytes]],
    monitors: dict[int, ConsoleMonitor],
) -> None:
    name, instance, cpu = RAW_CHILD
    bzimage = Path("/payload/bzImage")
    actual_bzimage_sha256 = hashlib.sha256(bzimage.read_bytes()).hexdigest()
    if actual_bzimage_sha256 != expected_bzimage_sha256:
        raise ScenarioFailure("raw-bzimage-sha256-mismatch")

    kerf(
        "create",
        name,
        f"--id={instance}",
        f"--cpus={cpu}",
        "--memory=256MB",
        stage="raw-bzimage-create",
    )
    _expect_status(name, "ready")
    cmdline = (
        "rdinit=/init quiet loglevel=6 panic=-1 "
        f"mk_transport_test=1 mk_instance_id={instance}"
    )
    load_raw_bzimage(
        bzimage,
        Path("/payload/secondary-initrd.cpio.gz"),
        cmdline,
        instance,
    )
    _expect_status(name, "loaded")

    console = _open_console(instance)
    consoles[instance] = console
    monitor = ConsoleMonitor(instance, console, True)
    monitors[instance] = monitor
    monitor.start()
    kerf("exec", name, stage="raw-bzimage-exec")
    _expect_status(name, "active")
    monitor.wait_for(
        lambda: monitor.ready_count >= 1,
        180,
        "raw-bzimage-ready",
    )
    monitor.wait_for(
        lambda: monitor.sequence_count >= 1,
        30,
        "raw-bzimage-console-control",
    )
    emit(
        "MK_TRANSPORT_RAW_BZIMAGE_PASS "
        f"instance={instance} loader=kexec_file_load status=active "
        f"ready={monitor.ready_count} sequences={monitor.sequence_count} "
        f"kernel_sha={kernel_sha} fixture_sha={fixture_sha} "
        f"image_sha256={actual_bzimage_sha256}"
    )

    kerf("kill", name, stage="raw-bzimage-kill")
    _expect_status(name, "loaded")
    monitor.close()
    console.close()
    del monitors[instance]
    del consoles[instance]
    kerf("unload", name, stage="raw-bzimage-unload")
    _expect_status(name, "ready")
    kerf("delete", name, stage="raw-bzimage-delete")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        process.communicate()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=RACE_REAP_TIMEOUT)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=RACE_REAP_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise ScenarioFailure("transport-race-command-unreaped") from error


def _wait_for_gate_ready(ready_fd: int, count: int, deadline: float) -> None:
    received = 0
    while received < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("race-start-gate", RACE_COMMAND_TIMEOUT)
        readable, _, _ = select.select([ready_fd], [], [], remaining)
        if not readable:
            raise subprocess.TimeoutExpired("race-start-gate", RACE_COMMAND_TIMEOUT)
        chunk = os.read(ready_fd, count - received)
        if not chunk:
            raise ScenarioFailure("transport-race-start-gate-closed")
        received += len(chunk)


def _run_gated_commands(
    commands: Mapping[str, Sequence[str]], operations: Sequence[str], cycle: int
) -> dict[str, subprocess.CompletedProcess[str]]:
    processes: dict[str, subprocess.Popen[str]] = {}
    start_fds: dict[str, tuple[int, int]] = {}
    ready_read, ready_write = os.pipe()
    deadline = time.monotonic() + RACE_COMMAND_TIMEOUT
    try:
        for operation in operations:
            start_read, start_write = os.pipe()
            start_fds[operation] = (start_read, start_write)
            processes[operation] = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    RACE_GATE_PROGRAM,
                    str(ready_write),
                    str(start_read),
                    *commands[operation],
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(ready_write, start_read),
            )
            os.close(start_read)
            start_fds[operation] = (-1, start_write)
        os.close(ready_write)
        ready_write = -1
        _wait_for_gate_ready(ready_read, len(operations), deadline)
        for operation in operations:
            os.write(start_fds[operation][1], b"1")
            os.close(start_fds[operation][1])
            start_fds[operation] = (-1, -1)
        results: dict[str, subprocess.CompletedProcess[str]] = {}
        for operation in operations:
            process = processes[operation]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, RACE_COMMAND_TIMEOUT)
            stdout, _ = process.communicate(timeout=remaining)
            results[operation] = subprocess.CompletedProcess(
                commands[operation], process.returncode, stdout, None
            )
        return results
    except subprocess.TimeoutExpired as error:
        raise ScenarioFailure(f"transport-race-command-timeout-{cycle}") from error
    finally:
        for descriptor in (ready_read, ready_write):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        for start_read, start_write in start_fds.values():
            for descriptor in (start_read, start_write):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        for process in processes.values():
            _stop_process(process)


def _run_race_commands(
    name: str, operations: Sequence[str], cycle: int
) -> dict[str, subprocess.CompletedProcess[str]]:
    return _run_gated_commands(
        {
            operation: [sys.executable, "-m", "kerf.cli", operation, name]
            for operation in operations
        },
        operations,
        cycle,
    )


def _race_exec_unload(name: str) -> dict[str, int]:
    """Race exec with file unload and normalize each valid outcome to loaded."""
    outcomes = {
        "exec_first_exec_wins": 0,
        "exec_first_unload_wins": 0,
        "unload_first_exec_wins": 0,
        "unload_first_unload_wins": 0,
    }
    for cycle in range(1, RACE_CYCLES + 1):
        operations = race_start_order(cycle)
        results = _run_race_commands(name, operations, cycle)
        exec_result = results["exec"]
        unload_result = results["unload"]

        if exec_result.returncode == 0 and unload_result.returncode == 0:
            raise ScenarioFailure("transport-race-double-success")
        status = (INSTANCES / name / "status").read_text().strip()
        order = "exec_first" if operations[0] == "exec" else "unload_first"
        if status == "active":
            if exec_result.returncode != 0 or unload_result.returncode == 0:
                raise ScenarioFailure("transport-race-active-result")
            winner = "exec"
            kerf("kill", name, stage=f"transport-race-kill-{cycle}")
            _expect_status(name, "loaded")
        elif status == "ready":
            if unload_result.returncode != 0 or exec_result.returncode == 0:
                raise ScenarioFailure("transport-race-ready-result")
            winner = "unload"
            _load_child(name, VICTIM)
        elif status == "loaded":
            raise ScenarioFailure("transport-race-no-winner")
        else:
            raise ScenarioFailure(f"transport-race-state-{status}")
        outcomes[f"{order}_{winner}_wins"] += 1
        emit(
            "MK_TRANSPORT_RACE_OUTCOME "
            f"cycle={cycle} start_order={order.replace('_', '-')} winner={winner}"
        )
    return outcomes


def run() -> None:
    values = _cmdline_values(Path("/proc/cmdline").read_text())
    kernel_sha = values.get("mk_transport_kernel_sha", "")
    fixture_sha = values.get("mk_transport_fixture_sha", "")
    bzimage_sha256 = values.get("mk_transport_bzimage_sha256", "")
    emit(
        f"MK_TRANSPORT_EVIDENCE kernel_sha={kernel_sha} "
        f"fixture_sha={fixture_sha} bzimage_sha256={bzimage_sha256}"
    )

    pool_base = _allocate_pool()
    Path("/run/transport-baseline.dts").write_text(
        render_baseline(
            pool_base,
            cpus=tuple(cpu for _name, _instance, cpu in ALL_CHILDREN),
            memory_bytes=0x40000000,
            resources=(),
        )
    )
    kerf("init", "--input=/run/transport-baseline.dts", stage="transport-init")

    consoles: dict[int, IO[bytes]] = {}
    monitors: dict[int, ConsoleMonitor] = {}
    try:
        _run_raw_bzimage_smoke(
            kernel_sha,
            fixture_sha,
            bzimage_sha256,
            consoles,
            monitors,
        )
        for name, instance, cpu in CHILDREN:
            kerf(
                "create",
                name,
                f"--id={instance}",
                f"--cpus={cpu}",
                "--memory=256MB",
                stage=f"transport-create-{instance}",
            )
            _expect_status(name, "ready")
            if instance == 1:
                _assert_malformed_vmlinux_rejected(name, instance)
                _load_ring_test_host()
            _load_child(name, instance)
            console = _open_console(instance)
            consoles[instance] = console
            monitor = ConsoleMonitor(instance, console, instance in SURVIVORS)
            monitors[instance] = monitor
            monitor.start()
            kerf("exec", name, stage=f"transport-exec-{instance}")
            _expect_status(name, "active")

        for monitor in monitors.values():
            monitor.wait_for(
                lambda monitor=monitor: monitor.ready_count >= 1,
                180,
                f"transport-ready-{monitor.instance}",
            )
        fairness_count = _wait_ring_test_host()
        monitors[1].wait_for(
            lambda: monitors[1].ring_full_warning_count >= 1,
            30,
            "ring-test-deferred-warning",
        )
        emit(
            "MK_RING_TEST_FIXTURE_PASS zero=1 max=4096 fifo=130 "
            f"concurrent=192 full_sent=63 fairness={fairness_count} "
            "unregister=1 peer=1 oversized=1"
        )
        for instance in SURVIVORS:
            monitors[instance].wait_for(
                lambda instance=instance: (
                    monitors[instance].sequence_count >= PHASE_SEQUENCES
                ),
                60,
                f"transport-baseline-{instance}",
            )

        baseline = {
            instance: monitors[instance].sequence_count for instance in SURVIVORS
        }
        victim_name = next(name for name, instance, _cpu in CHILDREN if instance == VICTIM)
        active_unload = kerf(
            "unload",
            victim_name,
            stage="transport-active-unload",
            check=False,
            capture=True,
        )
        if active_unload.returncode == 0:
            raise ScenarioFailure("transport-active-unload-accepted")
        _expect_status(victim_name, "active")
        emit(
            f"MK_TRANSPORT_ACTIVE_UNLOAD_REJECTED victim={VICTIM} status=active"
        )
        kerf("kill", victim_name, stage="transport-victim-kill")
        _expect_status(victim_name, "loaded")
        for instance in SURVIVORS:
            monitors[instance].wait_for(
                lambda instance=instance: (
                    monitors[instance].sequence_count
                    >= baseline[instance] + PHASE_SEQUENCES
                ),
                60,
                f"transport-during-kill-{instance}",
            )

        after_kill = {
            instance: monitors[instance].sequence_count for instance in SURVIVORS
        }
        kerf("unload", victim_name, stage="transport-victim-unload")
        _expect_status(victim_name, "ready")
        monitors[VICTIM].close()
        consoles[VICTIM].close()
        _load_child(victim_name, VICTIM)
        victim_console = _open_console(VICTIM)
        consoles[VICTIM] = victim_console
        victim_monitor = ConsoleMonitor(VICTIM, victim_console, False)
        monitors[VICTIM] = victim_monitor
        victim_monitor.start()
        kerf("exec", victim_name, stage="transport-victim-reexec")
        _expect_status(victim_name, "active")
        victim_monitor.wait_for(
            lambda: victim_monitor.ready_count >= 1,
            180,
            "transport-victim-restarted",
        )
        emit(
            f"MK_TRANSPORT_POST_PARK_RELOAD_PASS victim={VICTIM} status=active"
        )
        for instance in SURVIVORS:
            monitors[instance].wait_for(
                lambda instance=instance: (
                    monitors[instance].sequence_count
                    >= after_kill[instance] + PHASE_SEQUENCES
                ),
                60,
                f"transport-after-restart-{instance}",
            )

        before_stress = {
            instance: monitors[instance].sequence_count for instance in SURVIVORS
        }
        victim_monitor.close()
        victim_console.close()
        kerf("kill", victim_name, stage="transport-stress-victim-kill")
        _expect_status(victim_name, "loaded")
        outcomes = _race_exec_unload(victim_name)
        diagnostics = dmesg()
        for marker in KERNEL_DIAGNOSTIC_MARKERS:
            if marker in diagnostics:
                raise ScenarioFailure("transport-race-kernel-diagnostic")
        exec_wins = (
            outcomes["exec_first_exec_wins"]
            + outcomes["unload_first_exec_wins"]
        )
        unload_wins = (
            outcomes["exec_first_unload_wins"]
            + outcomes["unload_first_unload_wins"]
        )
        emit(
            "MK_TRANSPORT_EXEC_UNLOAD_STRESS_PASS "
            f"cycles={RACE_CYCLES} exec_wins={exec_wins} "
            f"unload_wins={unload_wins} terminal=loaded "
            f"exec_first_exec_wins={outcomes['exec_first_exec_wins']} "
            f"exec_first_unload_wins={outcomes['exec_first_unload_wins']} "
            f"unload_first_exec_wins={outcomes['unload_first_exec_wins']} "
            f"unload_first_unload_wins={outcomes['unload_first_unload_wins']}"
        )
        for instance in SURVIVORS:
            monitors[instance].wait_for(
                lambda instance=instance: (
                    monitors[instance].sequence_count
                    >= before_stress[instance] + PHASE_SEQUENCES
                ),
                60,
                f"transport-after-stress-{instance}",
            )

        minimum = min(monitors[instance].sequence_count for instance in SURVIVORS)
        if minimum < MINIMUM_SURVIVOR_SEQUENCES:
            raise ScenarioFailure("transport-minimum-sequences")
        emit(
            "MK_TRANSPORT_TEST_PASS "
            f"survivor_a={SURVIVORS[0]} survivor_b={SURVIVORS[1]} "
            f"victim={VICTIM} relaunch_count=1 minimum_sequences={minimum} "
            f"baseline_a={baseline[SURVIVORS[0]]} "
            f"baseline_b={baseline[SURVIVORS[1]]} "
            f"during_a={after_kill[SURVIVORS[0]]} "
            f"during_b={after_kill[SURVIVORS[1]]} "
            f"final_a={monitors[SURVIVORS[0]].sequence_count} "
            f"final_b={monitors[SURVIVORS[1]].sequence_count}"
        )
    finally:
        try:
            for name, _instance, _cpu in reversed(ALL_CHILDREN):
                path = INSTANCES / name
                if not path.exists():
                    continue
                status = (path / "status").read_text().strip()
                if status == "active":
                    kerf("kill", name, stage=f"transport-clean-kill-{name}")
                    _expect_status(name, "loaded")
                    status = "loaded"
                if status == "loaded":
                    kerf("unload", name, stage=f"transport-clean-unload-{name}")
                    _expect_status(name, "ready")
                kerf("delete", name, stage=f"transport-clean-delete-{name}")
        finally:
            for monitor in monitors.values():
                monitor.close()
            for console in consoles.values():
                console.close()


def main() -> int:
    try:
        run()
    except Exception as error:
        emit(f"MK_TRANSPORT_FAIL reason={type(error).__name__} detail={error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
