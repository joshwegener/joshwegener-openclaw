import unittest

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


if __name__ == "__main__":
    unittest.main()
