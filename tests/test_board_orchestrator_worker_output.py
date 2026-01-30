import unittest
import tempfile
from pathlib import Path

from scripts import board_orchestrator as bo


class TestWorkerOutputDetection(unittest.TestCase):
    def test_detect_worker_completion_with_patch_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "task-30.log"
            log_path.write_text("Patch file: `tmp/RecallDeck-Server-task30-api-contracts.patch`")

            result = bo.detect_worker_completion(30, str(log_path))

            self.assertIsNotNone(result)
            self.assertEqual(result.get("patchPath"), "tmp/RecallDeck-Server-task30-api-contracts.patch")

    def test_detect_worker_completion_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "task-30.log"
            log_path.write_text("Worker is still running.")

            result = bo.detect_worker_completion(30, str(log_path))

            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
