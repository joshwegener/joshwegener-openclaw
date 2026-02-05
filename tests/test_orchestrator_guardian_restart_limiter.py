import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.orchestrator_guardian import record_restart, restart_block_active, restart_limiter_allows


class TestOrchestratorGuardianRestartLimiter(unittest.TestCase):
    def test_restart_block_active(self) -> None:
        state = {"blockedUntilS": 2000}
        self.assertTrue(restart_block_active(state, now_epoch_s=1000))
        self.assertFalse(restart_block_active(state, now_epoch_s=2000))
        self.assertFalse(restart_block_active(state, now_epoch_s=3000))

    def test_restart_limiter_prunes_history(self) -> None:
        state = {"restartHistoryS": [1, 50, 99, 100]}
        # window=60s -> keeps [50, 99, 100] at now=100
        self.assertTrue(restart_limiter_allows(state, now_epoch_s=100, max_restarts=5, window_s=60))
        self.assertEqual(state["restartHistoryS"], [50, 99, 100])

    def test_restart_limiter_blocks_at_threshold(self) -> None:
        state = {}
        for t in [100, 101, 102]:
            record_restart(state, now_epoch_s=t, window_s=60)
        # 3 in window with max_restarts=3 should block further repairs.
        self.assertFalse(restart_limiter_allows(state, now_epoch_s=103, max_restarts=3, window_s=60))


if __name__ == "__main__":
    unittest.main()

