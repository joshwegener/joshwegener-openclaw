import os
import tempfile
import unittest
from pathlib import Path

from scripts import board_orchestrator as bo


class TestLeaseRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev_root = bo.WORKER_LEASE_ROOT
        bo.WORKER_LEASE_ROOT = self.tmp.name

    def tearDown(self) -> None:
        bo.WORKER_LEASE_ROOT = self.prev_root

    def test_recover_stale_lease_dir_archives(self) -> None:
        task_id = 60
        os.makedirs(bo.lease_dir(task_id))

        recovered = bo.recover_stale_lease_dir(task_id)

        self.assertTrue(recovered)
        self.assertFalse(os.path.isdir(bo.lease_dir(task_id)))
        self.assertTrue(os.path.isdir(bo.lease_archive_root(task_id)))
        self.assertTrue(bo.acquire_lease_dir(task_id))

    def test_recover_stale_lease_dir_ignores_valid_lease(self) -> None:
        task_id = 61
        os.makedirs(bo.lease_dir(task_id))
        Path(bo.lease_json_path(task_id)).write_text(
            '{\n'
            f'  "schemaVersion": {bo.LEASE_SCHEMA_VERSION},\n'
            f'  "taskId": {task_id},\n'
            '  "leaseId": "lease-123"\n'
            '}\n'
        )

        recovered = bo.recover_stale_lease_dir(task_id)

        self.assertFalse(recovered)
        self.assertTrue(os.path.isdir(bo.lease_dir(task_id)))


if __name__ == "__main__":
    unittest.main()
