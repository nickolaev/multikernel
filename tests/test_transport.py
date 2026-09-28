import os
import sys
import time
import unittest
from unittest import mock
from pathlib import Path
import struct
import subprocess
import tempfile

from harness.events import (
    EVENT_PREFIX,
    decode_event,
    encode_event,
    encode_marker,
    iter_events,
)
from harness.legacy_vmlinux import (
    CURRENT_NOTE_TYPE,
    LEGACY_NOTE_TYPE,
    LegacyNoteError,
    inspect_multikernel_vmlinux,
    patch_legacy_multikernel_note,
    patch_multikernel_entry,
)
from harness.raw_bzimage import (
    KEXEC_MULTIKERNEL,
    SYS_KEXEC_FILE_LOAD_X86_64,
    load_raw_bzimage,
    kexec_mk_id,
)
from harness.transport import (
    FAILURE_MARKERS,
    KERNEL_DIAGNOSTIC_MARKERS,
    RACE_CYCLES,
    SequenceTracker,
    TransportEvidenceError,
    race_start_order,
    transport_sequence_values,
    validate_transport_log,
)
from harness.primary import ScenarioFailure
from harness.transport_primary import (
    ConsoleMonitor,
    _assert_malformed_vmlinux_rejected,
    _run_gated_commands,
)
from harness.transport_secondary import _run_ring_test
from harness.transport_qemu import TransportConfig
from harness.transport_pins import SourcePin, SourcePinError, verify_source_pin


KERNEL_SHA = "1" * 40
FIXTURE_SHA = "2" * 40
BZIMAGE_SHA256 = "a" * 64


def complete_log(count: int = 96) -> str:
    events = [
        encode_event(
            "MK_TRANSPORT_EVIDENCE",
            {
                "kernel_sha": KERNEL_SHA,
                "fixture_sha": FIXTURE_SHA,
                "bzimage_sha256": BZIMAGE_SHA256,
            },
            "primary",
        )
    ]
    events.append(
        encode_event(
            "MK_TRANSPORT_RAW_BZIMAGE_PASS",
            {
                "instance": 4,
                "loader": "kexec_file_load",
                "status": "active",
                "ready": 1,
                "sequences": 1,
                "kernel_sha": KERNEL_SHA,
                "fixture_sha": FIXTURE_SHA,
                "image_sha256": BZIMAGE_SHA256,
            },
            "primary",
        )
    )
    for case in ("zero", "below-load", "interior-gap", "outside-load"):
        events.append(
            encode_event(
                "MK_TRANSPORT_ENTRY_REJECT_CASE_PASS",
                {
                    "instance": 1,
                    "case": case,
                    "attempts": 16,
                    "pool_segment_allocations": 0,
                    "status": "ready",
                },
                "primary",
            )
        )
    events.append(
        encode_event(
            "MK_TRANSPORT_ENTRY_REJECT_PASS",
            {
                "instance": 1,
                "entry_offset": 276,
                "valid_entry": 0x034D1AF3,
                "cases": 4,
                "attempts": 64,
                "pool_segment_allocations": 0,
                "status": "ready",
            },
            "primary",
        )
    )
    events.append(
        encode_event(
            "MK_TRANSPORT_LEGACY_REJECT_PASS",
            {
                "instance": 1,
                "note_offset": 148,
                "attempts": 16,
                "pool_segment_allocations": 0,
                "status": "ready",
            },
            "primary",
        )
    )
    events.append(
        encode_event(
            "MK_RING_TEST_FIXTURE_PASS",
            {
                "zero": 1,
                "max": 4096,
                "fifo": 130,
                "concurrent": 192,
                "full_sent": 63,
                "fairness": 128,
                "unregister": 1,
                "peer": 1,
                "oversized": 1,
            },
            "primary",
        )
    )
    for sequence in range(count):
        for instance in (1, 2):
            events.append(
                encode_event(
                    "MK_TRANSPORT_SEQUENCE",
                    {"instance": instance, "sequence": sequence},
                    "secondary",
                )
            )
    for cycle in range(1, RACE_CYCLES + 1):
        start_order = (
            "exec-first" if race_start_order(cycle)[0] == "exec" else "unload-first"
        )
        winner = "exec" if cycle % 4 in (1, 2) else "unload"
        events.append(
            encode_event(
                "MK_TRANSPORT_RACE_OUTCOME",
                {"cycle": cycle, "start_order": start_order, "winner": winner},
                "primary",
            )
        )
    events.extend(
        (
            encode_event(
                "MK_TRANSPORT_ACTIVE_UNLOAD_REJECTED",
                {"victim": 3, "status": "active"},
                "primary",
            ),
            encode_event(
                "MK_TRANSPORT_POST_PARK_RELOAD_PASS",
                {"victim": 3, "status": "active"},
                "primary",
            ),
            encode_event(
                "MK_TRANSPORT_EXEC_UNLOAD_STRESS_PASS",
                {
                    "cycles": 20,
                    "exec_wins": 10,
                    "unload_wins": 10,
                    "exec_first_exec_wins": 5,
                    "exec_first_unload_wins": 5,
                    "unload_first_exec_wins": 5,
                    "unload_first_unload_wins": 5,
                    "terminal": "loaded",
                },
                "primary",
            ),
        )
    )
    events.append(
        encode_event(
            "MK_TRANSPORT_TEST_PASS",
            {
                "survivor_a": 1,
                "survivor_b": 2,
                "victim": 3,
                "relaunch_count": 1,
                "minimum_sequences": count,
                "baseline_a": 32,
                "baseline_b": 32,
                "during_a": 64,
                "during_b": 64,
                "final_a": count,
                "final_b": count,
            },
            "primary",
        )
    )
    return (
        "multikernel: IPI ring full for instance 0\n"
        "multikernel: oversized IPI payload (4097 bytes) for instance 1\n"
        + "\n".join(events)
        + "\n"
    )


class SequenceTrackerTests(unittest.TestCase):
    def test_accepts_continuous_unique_sequence(self) -> None:
        tracker = SequenceTracker(2)
        for sequence in range(3):
            tracker.observe(
                {
                    "event": "MK_TRANSPORT_SEQUENCE",
                    "fields": {"instance": 2, "sequence": sequence},
                    "source": "secondary",
                }
            )
        self.assertEqual(tracker.count, 3)

    def test_rejects_gap_duplicate_and_wrong_instance(self) -> None:
        cases = (
            ({"instance": 1, "sequence": 1}, "sequence-discontinuity"),
            ({"instance": 2, "sequence": 0}, "wrong-instance"),
        )
        for fields, message in cases:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(TransportEvidenceError, message):
                    SequenceTracker(1).observe(
                        {
                            "event": "MK_TRANSPORT_SEQUENCE",
                            "fields": fields,
                            "source": "secondary",
                        }
                    )


class RaceGateTests(unittest.TestCase):
    def test_launches_both_commands_through_the_ready_gate(self) -> None:
        commands = {
            operation: [sys.executable, "-c", f"print('{operation}')"]
            for operation in ("exec", "unload")
        }
        for cycle in (1, 2):
            operations = race_start_order(cycle)
            with self.subTest(operations=operations):
                results = _run_gated_commands(commands, operations, cycle)
                self.assertEqual(results["exec"].stdout.strip(), "exec")
                self.assertEqual(results["unload"].stdout.strip(), "unload")

    def test_gate_timeout_reaps_both_command_groups(self) -> None:
        commands = {
            operation: [sys.executable, "-c", "import time; time.sleep(30)"]
            for operation in ("exec", "unload")
        }
        started = time.monotonic()
        with mock.patch("harness.transport_primary.RACE_COMMAND_TIMEOUT", 0.1), mock.patch(
            "harness.transport_primary.RACE_REAP_TIMEOUT", 0.1
        ):
            with self.assertRaisesRegex(ScenarioFailure, "command-timeout"):
                _run_gated_commands(commands, ("exec", "unload"), 1)
        self.assertLess(time.monotonic() - started, 2)


def _elf_with_linux_notes(note_types: tuple[int, ...]) -> bytes:
    image = bytearray(512)
    image[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", image, 32, 64)
    struct.pack_into("<H", image, 54, 56)
    struct.pack_into("<H", image, 56, 1)
    cursor = 128
    for note_type in note_types:
        struct.pack_into("<III", image, cursor, 6, 8, note_type)
        image[cursor + 12 : cursor + 18] = b"Linux\0"
        struct.pack_into("<Q", image, cursor + 20, 0x1234)
        cursor += 28
    struct.pack_into("<I", image, 64, 4)
    struct.pack_into("<Q", image, 72, 128)
    struct.pack_into("<Q", image, 96, cursor - 128)
    return bytes(image)


def _elf_with_loads_and_note(entry: int = 0x034D1AF3) -> bytes:
    image = bytearray(1024)
    image[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", image, 32, 64)
    struct.pack_into("<H", image, 54, 56)
    struct.pack_into("<H", image, 56, 3)

    struct.pack_into(
        "<IIQQQQQQ",
        image,
        64,
        1,
        5,
        0,
        0,
        0x01000000,
        0x01BB11E8,
        0x01BB11E8,
        0x200000,
    )
    struct.pack_into(
        "<IIQQQQQQ",
        image,
        120,
        1,
        6,
        0,
        0,
        0x02C00000,
        0x0B88000,
        0x0C50000,
        0x200000,
    )
    struct.pack_into(
        "<IIQQQQQQ", image, 176, 4, 4, 256, 0, 0, 28, 28, 4
    )
    struct.pack_into("<III", image, 256, 6, 8, CURRENT_NOTE_TYPE)
    image[268:274] = b"Linux\0"
    struct.pack_into("<Q", image, 276, entry)
    return bytes(image)


class LegacyVmlinuxTests(unittest.TestCase):
    def test_patches_exactly_one_current_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vmlinux"
            destination = Path(directory) / "vmlinux-legacy"
            original = _elf_with_linux_notes((CURRENT_NOTE_TYPE,))
            source.write_bytes(original)
            offset = patch_legacy_multikernel_note(source, destination)
            patched = destination.read_bytes()
        self.assertEqual(struct.unpack_from("<I", patched, offset)[0], LEGACY_NOTE_TYPE)
        self.assertEqual(original[:offset], patched[:offset])
        self.assertEqual(original[offset + 4 :], patched[offset + 4 :])

    def test_inspects_valid_entry_and_patches_malformed_entries(self) -> None:
        load_ranges_expected = (
            (0x01000000, 0x01BB11E8),
            (0x02C00000, 0x0B88000),
        )
        invalid_entries = (
            0,
            load_ranges_expected[0][0] - 1,
            sum(load_ranges_expected[0]),
            sum(load_ranges_expected[1]),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vmlinux"
            destination = Path(directory) / "vmlinux-malformed"
            original = _elf_with_loads_and_note()
            source.write_bytes(original)
            entry, load_ranges = inspect_multikernel_vmlinux(source)
            self.assertEqual(entry, 0x034D1AF3)
            self.assertEqual(load_ranges, load_ranges_expected)
            self.assertTrue(entry >= load_ranges_expected[1][0])
            self.assertLess(
                entry - load_ranges_expected[1][0],
                load_ranges_expected[1][1],
            )
            for invalid_entry in invalid_entries:
                with self.subTest(entry=invalid_entry):
                    offset = patch_multikernel_entry(
                        source, destination, invalid_entry
                    )
                    patched = destination.read_bytes()
                    self.assertEqual(offset, 276)
                    self.assertEqual(
                        struct.unpack_from("<Q", patched, offset)[0],
                        invalid_entry,
                    )
                    self.assertEqual(original[:offset], patched[:offset])
                    self.assertEqual(
                        original[offset + 8 :], patched[offset + 8 :]
                    )

    def test_producer_marker_passes_transport_validation(self) -> None:
        emitted: list[str] = []
        load_ranges = (
            (0x01000000, 0x01BB11E8),
            (0x02C00000, 0x0B88000),
        )
        with (
            mock.patch(
                "harness.transport_primary.inspect_multikernel_vmlinux",
                return_value=(0x034D1AF3, load_ranges),
            ),
            mock.patch(
                "harness.transport_primary.patch_multikernel_entry",
                return_value=276,
            ),
            mock.patch(
                "harness.transport_primary.patch_legacy_multikernel_note",
                return_value=264,
            ),
            mock.patch(
                "harness.transport_primary._assert_vmlinux_load_rejected"
            ) as reject,
            mock.patch(
                "harness.transport_primary.emit", side_effect=emitted.append
            ),
        ):
            _assert_malformed_vmlinux_rejected("transport-a", 1)

        self.assertEqual(reject.call_count, 80)
        entry_summary = next(
            marker
            for marker in emitted
            if marker.startswith("MK_TRANSPORT_ENTRY_REJECT_PASS ")
        )
        self.assertIn("cases=4", entry_summary)
        producer_names = {marker.split()[0] for marker in emitted}
        log_lines = []
        for line in complete_log().splitlines():
            if EVENT_PREFIX in line:
                event = decode_event(line)
                if str(event["event"]) in producer_names:
                    continue
            log_lines.append(line)
        producer_events = [
            str(encode_marker(marker, "primary")) for marker in emitted
        ]
        self.assertNotIn("None", producer_events)
        validate_transport_log("\n".join((*log_lines, *producer_events)))

    def test_rejects_missing_or_ambiguous_current_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vmlinux"
            destination = Path(directory) / "vmlinux-legacy"
            for note_types in ((), (CURRENT_NOTE_TYPE, CURRENT_NOTE_TYPE)):
                with self.subTest(note_types=note_types):
                    source.write_bytes(_elf_with_linux_notes(note_types))
                    with self.assertRaises(LegacyNoteError):
                        patch_legacy_multikernel_note(source, destination)


class TransportFixtureTests(unittest.TestCase):
    def test_secondary_initramfs_packages_transport_agent(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts/build-initramfs.sh"
        ).read_text()
        self.assertIn('${harness_package}/transport_secondary.py', script)

    def test_host_initramfs_packages_raw_bzimage_separately(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts/build-initramfs.sh"
        ).read_text()
        self.assertIn('${bzimage}', script)
        self.assertIn('${root}/payload/bzImage', script)
        makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text()
        self.assertIn("'$(SECONDARY_KERNEL)' '$(KERNEL)'", makefile)
        self.assertIn("PR7_LINUX_DIR ?=", makefile)
        self.assertIn("PR7_LINUX_DIR is required", makefile)
        self.assertIn(
            "override TRANSPORT_KERNEL_SHA := "
            "a7a784e3d11c7bfa8cee88c37a802bcd72fd58e9",
            makefile,
        )


class RawBzImageLoaderTests(unittest.TestCase):
    def test_passes_original_bzimage_fd_to_direct_syscall(self) -> None:
        calls: list[tuple[object, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            kernel = Path(directory) / "bzImage"
            initrd = Path(directory) / "initrd"
            image = bytearray(0x206)
            image[0x202:0x206] = b"HdrS"
            kernel.write_bytes(image)
            initrd.write_bytes(b"initrd")
            original = kernel.read_bytes()

            def syscall(*arguments: object) -> int:
                calls.append(arguments)
                self.assertEqual(os.pread(int(arguments[1]), 4, 0x202), b"HdrS")
                self.assertEqual(os.pread(int(arguments[2]), 6, 0), b"initrd")
                return 0

            flags = load_raw_bzimage(
                kernel,
                initrd,
                "rdinit=/init console=mktty0",
                4,
                syscall=syscall,
            )
            self.assertEqual(kernel.read_bytes(), original)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], SYS_KEXEC_FILE_LOAD_X86_64)
        self.assertEqual(calls[0][3], len(b"rdinit=/init console=mktty0") + 1)
        self.assertEqual(calls[0][5], KEXEC_MULTIKERNEL | kexec_mk_id(4))

    def test_rejects_non_bzimage_without_calling_syscall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = Path(directory) / "kernel"
            initrd = Path(directory) / "initrd"
            kernel.write_bytes(b"not-a-bzimage")
            initrd.write_bytes(b"initrd")
            with self.assertRaisesRegex(Exception, "not an x86 bzImage"):
                load_raw_bzimage(
                    kernel,
                    initrd,
                    "rdinit=/init",
                    4,
                    syscall=mock.Mock(side_effect=AssertionError("called")),
                )

    def test_ring_module_is_packaged_for_host_and_secondary(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts/build-initramfs.sh"
        ).read_text()
        self.assertEqual(script.count("mk_ring_test.ko"), 2)

    def test_first_two_children_run_sender_and_peer_ring_roles(self) -> None:
        with mock.patch("harness.transport_secondary.subprocess.run") as run:
            _run_ring_test(3)
            run.assert_not_called()
        for instance, role in ((1, "child"), (2, "peer")):
            with self.subTest(instance=instance), mock.patch(
                "harness.transport_secondary.subprocess.run"
            ) as run, mock.patch(
                "harness.transport_secondary.Path.read_text",
                side_effect=["0", "1"],
            ), mock.patch("harness.transport_secondary.time.sleep"):
                _run_ring_test(instance)
            self.assertIn(f"role={role}", run.call_args.args[0])
            self.assertTrue(run.call_args.kwargs["check"])

    def test_monitor_observes_deferred_ring_full_warning(self) -> None:
        read_fd, write_fd = os.pipe()
        console = os.fdopen(read_fd, "rb", buffering=0)
        monitor = ConsoleMonitor(1, console, False)
        monitor.start()
        try:
            with mock.patch("harness.transport_primary._relay"):
                os.write(
                    write_fd,
                    b"multikernel: IPI ring full for instance 0\n",
                )
                monitor.wait_for(
                    lambda: monitor.ring_full_warning_count == 1,
                    1,
                    "ring-warning",
                )
        finally:
            monitor.close()
            os.close(write_fd)
            console.close()

    def test_child_failure_wakes_monitor_without_idle_timeout(self) -> None:
        read_fd, write_fd = os.pipe()
        console = os.fdopen(read_fd, "rb", buffering=0)
        monitor = ConsoleMonitor(1, console, False)
        monitor.start()
        try:
            with mock.patch("harness.transport_primary._relay"):
                os.write(write_fd, b"MK_SECONDARY_FAIL reason=test\n")
                with self.assertRaisesRegex(ScenarioFailure, "transport-child-failed"):
                    monitor.wait_for(lambda: False, 1, "transport-child-failed")
        finally:
            monitor.close()
            os.close(write_fd)
            console.close()


class TransportLogTests(unittest.TestCase):
    def test_accepts_sha_bound_continuous_survivor_evidence(self) -> None:
        validate_transport_log(complete_log())

    def test_requires_raw_bzimage_sha_bound_readiness_evidence(self) -> None:
        missing = complete_log().replace(
            "MK_TRANSPORT_RAW_BZIMAGE_PASS",
            "MK_TRANSPORT_RAW_BZIMAGE_MISSING",
        )
        with self.assertRaisesRegex(TransportEvidenceError, "event-count"):
            validate_transport_log(missing)
        mismatched = complete_log().replace(BZIMAGE_SHA256, "b" * 64, 1)
        with self.assertRaisesRegex(
            TransportEvidenceError, "image-sha256-mismatch"
        ):
            validate_transport_log(mismatched)

    def test_recovers_sequence_from_interleaved_text_relay(self) -> None:
        split_record = (
            "MK_TRANSPORT_STR[  135.037612] kexec_file: Allocated 8192 bytes\n"
            "EAM instance=1:M[  135.037760] kexec_file: kexec_add_buffer\n"
            "K_EVENT {\"event\":\"MK_TRANSPORT_SEQUENCE\","
            "\"fields\":{\"instance\":1,\"sequence\":7787},"
            "\"source\":\"secondary\"}\n"
            "MK_TRANSPORT_STREAM instance=1:MK_TRANSPORT_SEQUENCE "
            "instance=1 sequence=7787\r\n"
            + encode_event(
                "MK_TRANSPORT_SEQUENCE",
                {"instance": 1, "sequence": 7788},
                "secondary",
            )
            + "\n"
        )
        self.assertEqual(
            transport_sequence_values(
                split_record, list(iter_events(split_record)), 1
            ),
            [7787, 7788],
        )

        log = complete_log()
        target = encode_event(
            "MK_TRANSPORT_SEQUENCE",
            {"instance": 1, "sequence": 30},
            "secondary",
        )
        replacement = split_record.replace("7787", "30").replace("7788", "31")
        replacement = replacement.rsplit("\n", 2)[0]
        validate_transport_log(log.replace(target, replacement, 1))

    def test_recovers_sequence_when_printk_splits_json_and_text_markers(self) -> None:
        split_record = (
            "MK_TRANSPORT_STR[  247.698606] kexec-vmlinux: entry\n"
            "EAM instance=2:M[  247.698747] kexec-vmlinux: offset\n"
            'K_EVENT {"event":"MK_TRANSPORT_S[  247.698896] loader\n'
            'EQUENCE","fields":{"instance":2,"sequence":6310}'
            '[  247.699254] purgatory\n'
            ',"source":"secondary"}\n'
            "MK_TRANSPORT_STREAM instance=2:MK_TRANS"
            "[  247.699698] locate hole\n"
            "[  247.700117] allocate\n"
            "PORT_SEQUENCE in[  247.700867] add buffer\n"
            "stance=2 sequence=6310\n"
        )
        self.assertEqual(transport_sequence_values(split_record, [], 2), [6310])

    def test_rejects_inconsistent_text_relay_instance(self) -> None:
        log = complete_log() + (
            "MK_TRANSPORT_STREAM instance=2:MK_TRANSPORT_SEQUENCE "
            "instance=1 sequence=96\n"
        )
        with self.assertRaisesRegex(TransportEvidenceError, "instance-mismatch"):
            validate_transport_log(log)

    def test_rejects_sequence_gap(self) -> None:
        log = complete_log().replace(
            '"instance":1,"sequence":30', '"instance":1,"sequence":31', 1
        )
        with self.assertRaisesRegex(TransportEvidenceError, "sequence-discontinuity"):
            validate_transport_log(log)

    def test_rejects_duplicate_sequence(self) -> None:
        log = complete_log().replace(
            '"instance":1,"sequence":31', '"instance":1,"sequence":30', 1
        )
        with self.assertRaisesRegex(TransportEvidenceError, "sequence-discontinuity"):
            validate_transport_log(log)

    def test_rejects_invalid_sha_and_failure_marker(self) -> None:
        with self.assertRaisesRegex(TransportEvidenceError, "invalid-sha"):
            validate_transport_log(complete_log().replace(KERNEL_SHA, "short"))
        for marker in FAILURE_MARKERS:
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(TransportEvidenceError, "failure-marker"):
                    validate_transport_log(complete_log() + marker + "\n")

    def test_generic_bug_marker_covers_serial_and_dmesg_validation(self) -> None:
        self.assertIn("BUG:", FAILURE_MARKERS)
        self.assertIn("BUG:", KERNEL_DIAGNOSTIC_MARKERS)

    def test_rejects_short_survivor_run(self) -> None:
        with self.assertRaisesRegex(TransportEvidenceError, "insufficient-pass-claim"):
            validate_transport_log(complete_log(count=95))

    def test_requires_active_rejection_and_post_park_reload(self) -> None:
        log = complete_log().replace(
            "MK_TRANSPORT_ACTIVE_UNLOAD_REJECTED",
            "MK_TRANSPORT_ACTIVE_UNLOAD_MISSING",
        )
        with self.assertRaisesRegex(TransportEvidenceError, "event-count"):
            validate_transport_log(log)

    def test_rejects_starved_survivor_phase(self) -> None:
        log = complete_log().replace('"during_a":64', '"during_a":63')
        with self.assertRaisesRegex(TransportEvidenceError, "survivor-starvation"):
            validate_transport_log(log)

    def test_rejects_incomplete_exec_unload_stress(self) -> None:
        log = complete_log().replace('"cycles":20', '"cycles":19', 1)
        with self.assertRaisesRegex(TransportEvidenceError, "race-stress-evidence"):
            validate_transport_log(log)

    def test_rejects_wrong_race_start_order_and_summary(self) -> None:
        wrong_order = complete_log().replace(
            '"start_order":"exec-first"', '"start_order":"unload-first"', 1
        )
        with self.assertRaisesRegex(TransportEvidenceError, "race-outcome"):
            validate_transport_log(wrong_order)
        wrong_summary = complete_log().replace(
            '"exec_first_exec_wins":5', '"exec_first_exec_wins":4', 1
        )
        with self.assertRaisesRegex(TransportEvidenceError, "race-summary-mismatch"):
            validate_transport_log(wrong_summary)


class TransportConfigTests(unittest.TestCase):
    def test_qemu_path_has_no_sriov_or_iommu_prerequisite(self) -> None:
        config = TransportConfig.from_environment(
            Path("/workspace/harness"),
            {
                "TRANSPORT_KERNEL_SHA": KERNEL_SHA,
                "TRANSPORT_FIXTURE_SHA": FIXTURE_SHA,
                "TRANSPORT_BZIMAGE_SHA256": BZIMAGE_SHA256,
                "TRANSPORT_KERF_SHA": "3" * 40,
                "TRANSPORT_LAZY_CMA_SHA": "4" * 40,
                "TRANSPORT_LINUX_DIR": "/src/linux",
                "TRANSPORT_FIXTURE_DIR": "/src/fixture",
                "TRANSPORT_KERF_DIR": "/src/kerf",
                "TRANSPORT_LAZY_CMA_DIR": "/src/lazy-cma",
                "QEMU_CPUS": "8",
                "QEMU_MEMORY_MB": "4096",
            },
        )
        args = " ".join(config.qemu_args())
        self.assertIn("mk_transport_test=1", args)
        self.assertIn(f"mk_transport_bzimage_sha256={BZIMAGE_SHA256}", args)
        self.assertNotIn("intel-iommu", args)
        self.assertNotIn("igb", args)

    def test_requires_full_kernel_and_fixture_shas(self) -> None:
        with self.assertRaisesRegex(Exception, "full 40-character SHA"):
            TransportConfig.from_environment(
                Path("/workspace/harness"),
                {
                    "TRANSPORT_KERNEL_SHA": "short",
                    "TRANSPORT_FIXTURE_SHA": FIXTURE_SHA,
                    "TRANSPORT_BZIMAGE_SHA256": BZIMAGE_SHA256,
                    "TRANSPORT_KERF_SHA": "3" * 40,
                    "TRANSPORT_LAZY_CMA_SHA": "4" * 40,
                },
            )


class SourcePinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.path)], check=True)
        subprocess.run(
            ["git", "-C", str(self.path), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.path), "config", "user.email", "test.invalid"],
            check=True,
        )
        (self.path / "tracked").write_text("clean\n")
        subprocess.run(["git", "-C", str(self.path), "add", "tracked"], check=True)
        subprocess.run(
            ["git", "-C", str(self.path), "commit", "-qm", "fixture"],
            check=True,
        )
        self.sha = subprocess.check_output(
            ["git", "-C", str(self.path), "rev-parse", "HEAD"], text=True
        ).strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accepts_exact_clean_absolute_checkout(self) -> None:
        self.assertEqual(
            verify_source_pin(SourcePin("linux", self.path, self.sha)), self.sha
        )

    def test_wraps_git_execution_and_timeout_errors(self) -> None:
        errors = (
            OSError("git unavailable"),
            subprocess.TimeoutExpired(["git"], 30),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with mock.patch(
                    "harness.transport_pins.subprocess.run",
                    side_effect=error,
                ):
                    with self.assertRaisesRegex(
                        SourcePinError, "git-execution-failed"
                    ):
                        verify_source_pin(SourcePin("linux", self.path, self.sha))

    def test_transport_preflight_requires_explicit_pr7_checkout(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["make", "-s", "transport-preflight", "PR7_LINUX_DIR="],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PR7_LINUX_DIR is required", result.stdout)

    def test_rejects_sha_mismatch_dirty_tree_and_relative_path(self) -> None:
        cases = (
            (SourcePin("linux", self.path, "0" * 40), "source-sha-mismatch"),
            (SourcePin("linux", Path("relative"), self.sha), "relative-source-path"),
        )
        for pin, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(SourcePinError, message):
                    verify_source_pin(pin)
        (self.path / "tracked").write_text("dirty\n")
        with self.assertRaisesRegex(SourcePinError, "dirty-source"):
            verify_source_pin(SourcePin("linux", self.path, self.sha))


if __name__ == "__main__":
    unittest.main()
