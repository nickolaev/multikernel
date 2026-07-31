import os
import unittest

from harness.console import wait_for_alive
from harness.events import encode_event, format_marker


class ConsoleMultiplexerTests(unittest.TestCase):
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
