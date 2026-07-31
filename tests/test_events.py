import unittest
from pathlib import Path

from harness.events import (
    EVENT_PREFIX,
    decode_event,
    encode_event,
    encode_marker,
    format_marker,
    iter_events,
)


class EventProtocolTests(unittest.TestCase):
    def test_formats_matching_text_and_json_events(self):
        fields = {"instance": 2, "vf": "0000:00:12.1"}

        self.assertEqual(
            format_marker("MK_SECONDARY_ALIVE", fields),
            "MK_SECONDARY_ALIVE instance=2 vf=0000:00:12.1",
        )
        payload = decode_event(encode_event("MK_SECONDARY_ALIVE", fields, "secondary"))
        self.assertEqual(payload["event"], "MK_SECONDARY_ALIVE")
        self.assertEqual(payload["fields"], fields)
        self.assertEqual(payload["source"], "secondary")

    def test_encodes_a_legacy_primary_marker(self):
        encoded = encode_marker(
            "MK_STAGE_IOMMU_DOMAIN id=1 owner=host mapped_regions=1",
            "primary",
        )

        self.assertIsNotNone(encoded)
        payload = decode_event(encoded or "")
        self.assertEqual(payload["event"], "MK_STAGE_IOMMU_DOMAIN")
        self.assertEqual(payload["fields"]["owner"], "host")

    def test_decodes_an_event_from_a_labeled_secondary_console(self):
        encoded = encode_event("MK_SECONDARY_ALIVE", {"instance": 1}, "secondary")
        line = f"MK_SECONDARY_STREAM:{encoded}"

        self.assertEqual(list(iter_events(line))[0]["source"], "secondary")

    def test_recovers_from_timestamped_kernel_console_insertion(self):
        first = encode_event("MK_STAGE_READY", {"instance": 1}, "primary")
        first = first.replace(
            '"source"',
            '"s[   66.998792] igb: diagnostic kernel message\nource"',
        )
        second = encode_event("MK_STAGE_DONE", {"instance": 1}, "primary")

        events = list(iter_events(f"{first}\n{second}\n"))

        self.assertEqual(
            [event["event"] for event in events],
            ["MK_STAGE_READY", "MK_STAGE_DONE"],
        )

    def test_rejects_malformed_events(self):
        with self.assertRaisesRegex(ValueError, "prefix"):
            decode_event("MK_STAGE_READY id=1")
        with self.assertRaises(ValueError):
            decode_event(EVENT_PREFIX + "[]")
        with self.assertRaises(ValueError):
            list(iter_events(EVENT_PREFIX + '{"event":'))

    def test_normalizes_path_like_host_fields(self):
        payload = decode_event(
            encode_event("MK_QEMU_START", {"log": Path("/tmp/qemu.log")}, "host")
        )
        self.assertEqual(payload["fields"]["log"], "/tmp/qemu.log")


if __name__ == "__main__":
    unittest.main()
