import os
import unittest

from harness.console import ConsoleEventReader, wait_for_alive
from harness.events import encode_event, format_marker


class ConsoleMultiplexerTests(unittest.TestCase):
    def test_event_reader_preserves_a_partial_structured_record(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        observed = []
        stream = ConsoleEventReader(reader, observed.append)
        try:
            fields = {"instance": 1, "reads": 1024}
            payload = encode_event(
                "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS",
                fields,
                "secondary",
            ).encode()
            midpoint = len(payload) // 2
            os.write(write_fd, payload[:midpoint])
            self.assertIsNone(
                stream.drain_for_event(
                    "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS"
                )
            )

            os.write(write_fd, payload[midpoint:] + b"\n")
            event = stream.wait_for_event(
                "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS",
                timeout=0.1,
            )

            self.assertIsNotNone(event)
            self.assertEqual(event["fields"]["reads"], 1024)
            self.assertEqual(len(observed), 1)
        finally:
            reader.close()
            os.close(write_fd)

    def test_event_reader_drains_to_the_newest_matching_record(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        observed = []
        stream = ConsoleEventReader(reader, observed.append)
        try:
            lines = []
            for reads in (1024, 2048):
                fields = {"instance": 1, "reads": reads}
                lines.extend(
                    [
                        encode_event(
                            "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS",
                            fields,
                            "secondary",
                        ),
                        format_marker(
                            "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS",
                            fields,
                        ),
                    ]
                )
            os.write(write_fd, ("\n".join(lines) + "\n").encode())

            event = stream.wait_for_event(
                "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS",
                timeout=0.1,
                predicate=lambda item: item["fields"]["reads"] > 512,
                drain=True,
            )

            self.assertIsNotNone(event)
            self.assertEqual(event["fields"]["reads"], 2048)
            self.assertEqual(len(observed), 4)
        finally:
            reader.close()
            os.close(write_fd)

    def test_event_reader_quiet_drain_includes_an_inflight_record(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        observed = []
        event_name = "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS"
        first = encode_event(
            event_name,
            {"instance": 1, "reads": 1024},
            "secondary",
        )
        delayed = encode_event(
            event_name,
            {"instance": 1, "reads": 2048},
            "secondary",
        )

        def observe_and_release_inflight(line):
            observed.append(line)
            if len(observed) == 1:
                os.write(write_fd, f"{delayed}\n".encode())

        stream = ConsoleEventReader(reader, observe_and_release_inflight)
        try:
            os.write(write_fd, f"{first}\n".encode())

            event = stream.drain_for_event(
                event_name,
                idle_timeout=0.05,
                timeout=0.2,
            )

            self.assertIsNotNone(event)
            self.assertEqual(event["fields"]["reads"], 2048)
            self.assertEqual(len(observed), 2)
        finally:
            reader.close()
            os.close(write_fd)

    def test_event_reader_reports_when_quiet_drain_times_out(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        event_name = "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS"
        payload = encode_event(
            event_name,
            {"instance": 1, "reads": 1024},
            "secondary",
        )

        def keep_console_busy(_line):
            os.write(write_fd, f"{payload}\n".encode())

        stream = ConsoleEventReader(reader, keep_console_busy)
        try:
            os.write(write_fd, f"{payload}\n".encode())

            with self.assertRaises(TimeoutError):
                stream.drain_for_event(
                    event_name,
                    idle_timeout=0.05,
                    timeout=0.01,
                )
        finally:
            reader.close()
            os.close(write_fd)

    def test_waits_for_json_and_text_from_every_console(self):
        readers = {}
        writers = []
        try:
            for instance in (1, 2):
                read_fd, write_fd = os.pipe()
                readers[instance] = os.fdopen(read_fd, "rb", buffering=0)
                writers.append(write_fd)
                fields = {"instance": instance, "pid": 30 + instance}
                payload = "\n".join(
                    [
                        encode_event("MK_SECONDARY_ALIVE", fields, "secondary"),
                        format_marker("MK_SECONDARY_ALIVE", fields),
                        "",
                    ]
                )
                os.write(write_fd, payload.encode())

            observed = []
            self.assertTrue(
                wait_for_alive(
                    readers,
                    timeout=0.1,
                    on_line=lambda instance, line: observed.append((instance, line)),
                )
            )
            self.assertEqual(len(observed), 4)
        finally:
            for reader in readers.values():
                reader.close()
            for writer in writers:
                os.close(writer)

    def test_does_not_accept_a_text_marker_without_json(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            os.write(write_fd, b"MK_SECONDARY_ALIVE instance=1 pid=31\n")
            self.assertFalse(
                wait_for_alive({1: reader}, timeout=0.01, on_line=lambda *_: None)
            )
        finally:
            reader.close()
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
