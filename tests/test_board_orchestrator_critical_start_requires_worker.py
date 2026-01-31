import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import board_orchestrator as bo


class FakeKanboard:
    def __init__(self, *, repo_path: str):
        self.pid = 1
        self.repo_path = repo_path

        # Column ids
        self.col_backlog = 10
        self.col_ready = 11
        self.col_wip = 12
        self.col_review = 13
        self.col_blocked = 14
        self.col_paused = 15
        self.col_done = 16

        self.task = {
            "id": 57,
            "title": "Orchestrator: CRITICAL-start must spawn worker handle",
            "description": f"Repo: {repo_path}\n",
            "column_id": self.col_ready,
            "swimlane_id": 1,
            "position": 1,
        }
        self.tags_by_task_id = {57: ["critical", "story"]}
        self.moves = []  # (task_id, column_id)

    def rpc(self, method, params=None):
        if method == "getProjectByName":
            return {"id": self.pid}
        if method == "getBoard":
            # Minimal board: one swimlane with standard columns.
            return [
                {
                    "id": 1,
                    "name": "Default swimlane",
                    "columns": [
                        {"id": self.col_backlog, "title": "Backlog", "tasks": []},
                        {"id": self.col_ready, "title": "Ready", "tasks": [self._task_card()]},
                        {"id": self.col_wip, "title": "Work in progress", "tasks": []},
                        {"id": self.col_review, "title": "Review", "tasks": []},
                        {"id": self.col_blocked, "title": "Blocked", "tasks": []},
                        {"id": self.col_paused, "title": "Paused", "tasks": []},
                        {"id": self.col_done, "title": "Done", "tasks": []},
                    ],
                }
            ]
        if method == "getTask":
            # board_orchestrator uses params=[task_id]
            task_id = int(params[0])
            if task_id != 57:
                raise KeyError(task_id)
            return dict(self.task)
        if method == "getTaskTags":
            task_id = int(params.get("task_id"))
            tags = self.tags_by_task_id.get(task_id, [])
            # Kanboard returns {tag_id: tag_name}
            return {str(i + 1): t for i, t in enumerate(tags)}
        if method == "setTaskTags":
            _pid, task_id, tags = params
            self.tags_by_task_id[int(task_id)] = list(tags)
            return True
        if method == "moveTaskPosition":
            task_id = int(params["task_id"])
            col_id = int(params["column_id"])
            if task_id == 57:
                self.task["column_id"] = col_id
            self.moves.append((task_id, col_id))
            return True

        raise NotImplementedError(method)

    def _task_card(self):
        # Card objects returned by getBoard are a subset.
        return {
            "id": self.task["id"],
            "title": self.task["title"],
            "position": self.task["position"],
        }


class TestCriticalStartRequiresWorker(unittest.TestCase):
    def test_critical_not_moved_to_wip_when_worker_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            fake = FakeKanboard(repo_path=str(repo))

            # Seed orchestrator state so we don't enter first-run dry-run.
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            # Patch globals for isolated run.
            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn = bo.WORKER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.WORKER_SPAWN_CMD = ""  # cannot start worker

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()

                self.assertEqual(rc, 0)
                out = buf.getvalue().strip()
                payload = json.loads(out)

                # Should tag paused:missing-worker and NOT move to WIP.
                self.assertTrue(any("paused:missing-worker" in a for a in payload.get("actions", [])))
                self.assertEqual(fake.task["column_id"], fake.col_ready)
                self.assertFalse(any(col == fake.col_wip for (_tid, col) in fake.moves))

                tags = {t.lower() for t in fake.tags_by_task_id[57]}
                self.assertIn("paused", tags)
                self.assertIn("paused:missing-worker", tags)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.WORKER_SPAWN_CMD = old_spawn


if __name__ == "__main__":
    unittest.main()
