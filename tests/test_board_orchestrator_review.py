import unittest
import hashlib
import json
import tempfile
from pathlib import Path

from scripts import board_orchestrator as bo


class TestReviewResultParsing(unittest.TestCase):
    def test_parse_review_result_json(self) -> None:
        text = "Some log\nREVIEW_RESULT: {\"score\": 92, \"verdict\": \"PASS\", \"notes\": \"Looks good\"}\n"
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 92)
        self.assertEqual(result.get("verdict"), "PASS")
        self.assertEqual(result.get("notes"), "Looks good")

    def test_parse_review_result_kv(self) -> None:
        text = "REVIEW_RESULT: score=77 verdict=REWORK"
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 77)
        self.assertEqual(result.get("verdict"), "REWORK")

    def test_parse_review_result_invalid(self) -> None:
        text = "REVIEW_RESULT: verdict=PASS"
        self.assertIsNone(bo.parse_review_result(text))

    def test_parse_review_result_lowercase_and_space(self) -> None:
        text = "review result: score=88 verdict=pass"
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 88)
        self.assertEqual(result.get("verdict"), "PASS")

    def test_parse_review_result_last_match_wins(self) -> None:
        text = (
            "review_result: score=91 verdict=PASS\n"
            "noise\n"
            "REVIEW_RESULT: score=70 verdict=REWORK\n"
        )
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 70)
        self.assertEqual(result.get("verdict"), "REWORK")


class TestDetectReviewResult(unittest.TestCase):
    def test_detect_review_result_after_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "review.log"
            stale = json.dumps({"score": 60, "verdict": "REWORK"})
            fresh = json.dumps({"score": 95, "verdict": "PASS"})
            p.write_text(
                "\n".join(
                    [
                        "review_result: " + stale,
                        "### REVIEW START 2026-01-31T00:00:00Z",
                        "review_result: " + fresh,
                    ]
                )
            )
            result = bo.detect_review_result(1, str(p))
            self.assertIsNotNone(result)
            self.assertEqual(result.get("score"), 95)
            self.assertEqual(result.get("verdict"), "PASS")


class TestReviewRevisionHelpers(unittest.TestCase):
    def test_compute_patch_revision_none_and_missing(self) -> None:
        self.assertIsNone(bo.compute_patch_revision(None))
        self.assertIsNone(bo.compute_patch_revision("/does/not/exist.patch"))

    def test_compute_patch_revision_hashes_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.patch"
            content = b"diff --git a/foo b/foo\n"
            p.write_bytes(content)
            expected = hashlib.sha256(content).hexdigest()
            self.assertEqual(bo.compute_patch_revision(str(p)), expected)

    def test_extract_review_revision_variants(self) -> None:
        self.assertEqual(bo.extract_review_revision({"reviewRevision": "abc"}), "abc")
        self.assertEqual(bo.extract_review_revision({"review_revision": "def"}), "def")
        self.assertEqual(bo.extract_review_revision({"revision": "ghi"}), "ghi")
        self.assertIsNone(bo.extract_review_revision({"score": 90}))
        self.assertIsNone(bo.extract_review_revision("not-a-dict"))

    def test_review_revision_matches(self) -> None:
        self.assertTrue(bo.review_revision_matches(None, None))
        self.assertTrue(bo.review_revision_matches(None, "abc"))
        self.assertFalse(bo.review_revision_matches("abc", None))
        self.assertTrue(bo.review_revision_matches("abc", "abc"))
        self.assertFalse(bo.review_revision_matches("abc", "def"))


if __name__ == "__main__":
    unittest.main()
