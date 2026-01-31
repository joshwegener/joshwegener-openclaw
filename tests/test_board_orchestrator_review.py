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

    def test_parse_review_result_embedded_json(self) -> None:
        embedded = '{"score": 93, "verdict": "PASS", "notes": "ok"}'
        text = 'review_result: {"type": "result", "result": "' + embedded.replace('"', '\\"') + '"}'
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 93)
        self.assertEqual(result.get("verdict"), "PASS")
        self.assertEqual(result.get("notes"), "ok")

    def test_parse_review_result_review_revision(self) -> None:
        text = 'review_result: {"score": 90, "verdict": "PASS", "review_revision": "abc123"}'
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("reviewRevision"), "abc123")

    def test_parse_review_result_critical_items_string(self) -> None:
        text = 'REVIEW_RESULT: {"score": 65, "verdict": "REWORK", "critical_items": "Missing tests"}'
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("critical_items"), ["Missing tests"])

    def test_parse_review_result_critical_items_camel_list(self) -> None:
        text = 'REVIEW_RESULT: {"score": 70, "verdict": "REWORK", "criticalItems": ["Bad index", "Missing retry"]}'
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("critical_items"), ["Bad index", "Missing retry"])

    def test_parse_review_result_uppercase_space_sentinel(self) -> None:
        text = "REVIEW RESULT: score=82 verdict=PASS"
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 82)
        self.assertEqual(result.get("verdict"), "PASS")

    def test_parse_review_result_with_trailing_noise(self) -> None:
        text = (
            "noise\n"
            "review_result: {\"score\": 90, \"verdict\": \"PASS\"}\n"
            "more noise\n"
        )
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 90)
        self.assertEqual(result.get("verdict"), "PASS")

    def test_parse_review_result_json_on_next_line(self) -> None:
        text = "REVIEW_RESULT:\n{\"score\": 88, \"verdict\": \"PASS\"}\n"
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 88)
        self.assertEqual(result.get("verdict"), "PASS")

    def test_parse_review_result_fenced_json(self) -> None:
        text = (
            "review_result: ```json\n"
            "{\"score\": 89, \"verdict\": \"PASS\"}\n"
            "```\n"
        )
        result = bo.parse_review_result(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("score"), 89)
        self.assertEqual(result.get("verdict"), "PASS")


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
