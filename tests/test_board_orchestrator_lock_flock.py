import multiprocessing
import os
import tempfile
import time
import unittest


def _try_lock(lock_path: str, hold_s: float, q: "multiprocessing.Queue[bool]") -> None:
    # Import inside the subprocess so module globals are isolated per-process.
    from scripts import board_orchestrator as bo

    if bo.fcntl is None:
        q.put(False)
        return

    bo.LOCK_PATH = lock_path
    bo.LOCK_STRATEGY = "flock"
    bo.LOCK_WAIT_MS = 0

    lock = bo.acquire_lock("test-run")
    q.put(lock is not None)
    if lock is None:
        return
    try:
        time.sleep(hold_s)
    finally:
        bo.release_lock(lock)


class TestLockFlock(unittest.TestCase):
    def test_flock_prevents_overlapping_runs(self) -> None:
        from scripts import board_orchestrator as bo

        if bo.fcntl is None:
            self.skipTest("fcntl not available; flock lock strategy unsupported on this platform")

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "board-orchestrator.lock")

            q1: "multiprocessing.Queue[bool]" = multiprocessing.Queue()
            p1 = multiprocessing.Process(target=_try_lock, args=(lock_path, 0.8, q1))
            p1.start()
            acquired_1 = q1.get(timeout=2)
            self.assertTrue(acquired_1)

            # While p1 is holding the lock, a second process must not acquire it.
            q2: "multiprocessing.Queue[bool]" = multiprocessing.Queue()
            p2 = multiprocessing.Process(target=_try_lock, args=(lock_path, 0.0, q2))
            p2.start()
            acquired_2 = q2.get(timeout=2)
            self.assertFalse(acquired_2)

            p2.join(timeout=2)
            p1.join(timeout=3)
            self.assertEqual(p1.exitcode, 0)
            self.assertEqual(p2.exitcode, 0)

            # After the first lock is released, acquisition should succeed again.
            q3: "multiprocessing.Queue[bool]" = multiprocessing.Queue()
            p3 = multiprocessing.Process(target=_try_lock, args=(lock_path, 0.0, q3))
            p3.start()
            acquired_3 = q3.get(timeout=2)
            p3.join(timeout=2)
            self.assertTrue(acquired_3)
            self.assertEqual(p3.exitcode, 0)


if __name__ == "__main__":
    unittest.main()

