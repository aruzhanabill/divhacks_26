from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image

from app.reports import (
    classify_from_caption,
    coords_from_caption,
    coords_from_known_place,
    coords_from_intersection,
    gps_from_image,
    headline_for,
    _parse_vision_json,
)


def _jpeg() -> bytes:
    image = Image.new("RGB", (8, 8), color=(30, 30, 30))
    buf = BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


class ReportHelpersTest(unittest.TestCase):
    def test_headline_car_crash(self) -> None:
        self.assertEqual(headline_for("incident", "traffic", "car crash on broadway", None), "Car crash")
        result = classify_from_caption("car crash on broadway")
        assert result is not None
        self.assertEqual(result["kind"], "incident")
        self.assertEqual(result["category"], "traffic")
        self.assertEqual(result["headline"], "Car crash")

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

    def test_columbia_gates_is_a_known_place(self) -> None:
        coords = coords_from_known_place("car crash in front of columbia 116th broadway gates")
        assert coords is not None
        self.assertAlmostEqual(coords[0], 40.80754, places=4)
        self.assertAlmostEqual(coords[1], -73.96257, places=4)

    def test_112th_and_broadway(self) -> None:
        from app.reports import resolve_location

        coords = coords_from_intersection("car crash 112th and broadway")
        assert coords is not None
        self.assertGreater(coords[0], 40.8045)
        self.assertLess(coords[0], 40.8065)
        self.assertLess(coords[1], -73.9655)
        follow = coords_from_intersection("broadway and 112th")
        assert follow is not None
        self.assertAlmostEqual(follow[0], coords[0], places=4)
        resolved = resolve_location(None, "car crash 112th and broadway", None, None, None)
        assert resolved is not None
        coords = coords_from_known_place("broadway and 116th")
        assert coords is not None
        self.assertAlmostEqual(coords[0], 40.80754, places=4)

    def test_109th_and_columbus_is_not_112th(self) -> None:
        from app.reports import coords_from_intersection

        at_109 = coords_from_intersection("stabbing 109th and columbus ave")
        at_112 = coords_from_intersection("112th and columbus")
        assert at_109 is not None and at_112 is not None
        self.assertLess(at_109[0], at_112[0])
        self.assertLess(at_109[0], 40.8020)
        self.assertGreater(at_109[0], 40.7990)
        self.assertLess(at_109[1], -73.9595)

    def test_sentence_resolves_without_coordinates(self) -> None:
        from app.reports import resolve_location

        coords = resolve_location(
            None,
            "car crash in front of columbia 116th broadway gates",
            None,
            None,
            None,
        )
        assert coords is not None
        self.assertAlmostEqual(coords[0], 40.80754, places=4)


if __name__ == "__main__":
    unittest.main()
