import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.orchestrator_guardian_lib import is_heartbeat_stale


class TestOrchestratorGuardianHeartbeatStaleness(unittest.TestCase):
    def test_missing_heartbeat_is_stale(self) -> None:
        self.assertTrue(is_heartbeat_stale(None, now_s=1000, tick_seconds=20, factor=3))

    def test_recent_heartbeat_is_not_stale(self) -> None:
        hb = {"tsEpochS": 995, "pid": 123, "version": "x"}
        self.assertFalse(is_heartbeat_stale(hb, now_s=1000, tick_seconds=20, factor=3))

    def test_stale_heartbeat_is_stale(self) -> None:
        # threshold = 3 * 20 = 60s
        hb = {"tsEpochS": 940, "pid": 123, "version": "x"}
        self.assertTrue(is_heartbeat_stale(hb, now_s=1000, tick_seconds=20, factor=3))

    def test_iso_timestamp_parses(self) -> None:
        # 1970-01-01T00:16:40Z -> 1000
        hb = {"ts": "1970-01-01T00:16:40Z", "pid": 123, "version": "x"}
        self.assertFalse(is_heartbeat_stale(hb, now_s=1010, tick_seconds=20, factor=3))


if __name__ == "__main__":
    unittest.main()
