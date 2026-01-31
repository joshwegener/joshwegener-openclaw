import unittest

from scripts import board_orchestrator as bo


class TestLeaseLiveness(unittest.TestCase):
    def test_missing_pid_recent_lease_is_unknown(self) -> None:
        old_grace = bo.LEASE_STALE_GRACE_MS
        try:
            bo.LEASE_STALE_GRACE_MS = 10_000
            lease = bo.init_lease_payload(
                42,
                "run-1",
                "repo",
                "/tmp/repo",
                "/tmp/log",
                "/tmp/patch",
                "/tmp/comment",
                "spawn-cmd",
                60,
            )
            verdict, _pid, note = bo.evaluate_lease_liveness(42, lease)
            self.assertEqual(verdict, "unknown")
            self.assertEqual(note, bo.LEASE_PENDING_NOTE)
        finally:
            bo.LEASE_STALE_GRACE_MS = old_grace

    def test_missing_pid_old_lease_is_dead(self) -> None:
        old_grace = bo.LEASE_STALE_GRACE_MS
        try:
            bo.LEASE_STALE_GRACE_MS = 1000
            lease = bo.init_lease_payload(
                43,
                "run-2",
                "repo",
                "/tmp/repo",
                "/tmp/log",
                "/tmp/patch",
                "/tmp/comment",
                "spawn-cmd",
                60,
            )
            stale_ms = bo.now_ms() - (bo.LEASE_STALE_GRACE_MS + 2000)
            lease["createdAtMs"] = stale_ms
            lease["updatedAtMs"] = stale_ms
            verdict, _pid, note = bo.evaluate_lease_liveness(43, lease)
            self.assertEqual(verdict, "dead")
            self.assertEqual(note, "missing worker pid")
        finally:
            bo.LEASE_STALE_GRACE_MS = old_grace


if __name__ == "__main__":
    unittest.main()
