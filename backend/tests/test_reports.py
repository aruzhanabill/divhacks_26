from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image

from app.reports import classify_from_caption, coords_from_caption, gps_from_image, _parse_vision_json


def _jpeg() -> bytes:
    image = Image.new("RGB", (8, 8), color=(30, 30, 30))
    buf = BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


class ReportHelpersTest(unittest.TestCase):
    def test_crash_caption(self) -> None:
        result = classify_from_caption("car crash on broadway")
        assert result is not None
        self.assertEqual(result["kind"], "incident")
        self.assertEqual(result["category"], "traffic")

    def test_lamp_caption(self) -> None:
        result = classify_from_caption("broken street lamp near campus")
        assert result is not None
        self.assertEqual(result["kind"], "street_light")

    def test_coords_in_caption(self) -> None:
        self.assertEqual(coords_from_caption("at 40.8075, -73.9626 tonight"), (40.8075, -73.9626))

    def test_plain_jpeg_has_no_gps(self) -> None:
        self.assertIsNone(gps_from_image(_jpeg()))

    def test_vision_json_fence(self) -> None:
        parsed = _parse_vision_json('```json\n{"kind": "incident", "category": "traffic"}\n```')
        self.assertEqual(parsed["category"], "traffic")

    def test_caption_without_place_asks_for_location(self) -> None:
        from app.reports import ingest_photo_report

        result = ingest_photo_report(image=None, caption="car crash")
        self.assertEqual(result["status"], "need_location")


if __name__ == "__main__":
    unittest.main()
