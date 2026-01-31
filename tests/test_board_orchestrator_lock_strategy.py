import os
import tempfile
import unittest

from scripts import board_orchestrator as bo


class TestLockStrategy(unittest.TestCase):
    def test_unknown_strategy_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_strategy = bo.LOCK_STRATEGY
            old_path = bo.LOCK_PATH
            try:
                bo.LOCK_STRATEGY = "mystery"
                bo.LOCK_PATH = os.path.join(tmp, "lock.json")
                lock = bo.acquire_lock("run-1")
                self.assertIsNone(lock)
                self.assertFalse(os.path.exists(bo.LOCK_PATH))
            finally:
                bo.LOCK_STRATEGY = old_strategy
                bo.LOCK_PATH = old_path


if __name__ == "__main__":
    unittest.main()
