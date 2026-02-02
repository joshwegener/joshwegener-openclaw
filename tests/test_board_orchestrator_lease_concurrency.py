import multiprocessing
import os
import shutil
import tempfile
import time
import unittest


def _try_acquire_lease(root: str, task_id: int, hold_s: float, q: "multiprocessing.Queue[bool]") -> None:
    from scripts import board_orchestrator as bo

    bo.WORKER_LEASE_ROOT = root
    acquired = bo.acquire_lease_dir(task_id)
    q.put(acquired)
    if not acquired:
        return
    try:
        time.sleep(hold_s)
    finally:
        # Best-effort cleanup so follow-on tests don't see stale state.
        try:
            shutil.rmtree(bo.lease_dir(task_id))
        except Exception:
            pass


class TestLeaseConcurrency(unittest.TestCase):
    def test_atomic_mkdir_enforces_single_lease_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_id = 123

            q1: "multiprocessing.Queue[bool]" = multiprocessing.Queue()
            p1 = multiprocessing.Process(target=_try_acquire_lease, args=(tmp, task_id, 0.8, q1))
            p1.start()
            acquired_1 = q1.get(timeout=2)
            self.assertTrue(acquired_1)

            q2: "multiprocessing.Queue[bool]" = multiprocessing.Queue()
            p2 = multiprocessing.Process(target=_try_acquire_lease, args=(tmp, task_id, 0.0, q2))
            p2.start()
            acquired_2 = q2.get(timeout=2)
            self.assertFalse(acquired_2)

            p2.join(timeout=2)
            p1.join(timeout=3)
            self.assertEqual(p1.exitcode, 0)
            self.assertEqual(p2.exitcode, 0)

            # After cleanup, acquisition should succeed again.
            q3: "multiprocessing.Queue[bool]" = multiprocessing.Queue()
            p3 = multiprocessing.Process(target=_try_acquire_lease, args=(tmp, task_id, 0.0, q3))
            p3.start()
            acquired_3 = q3.get(timeout=2)
            p3.join(timeout=2)
            self.assertTrue(acquired_3)
            self.assertEqual(p3.exitcode, 0)

            # Sanity: task dir should exist (lease creation implies task root creation).
            self.assertTrue(os.path.isdir(os.path.join(tmp, f"task-{task_id}")))


if __name__ == "__main__":
    unittest.main()

