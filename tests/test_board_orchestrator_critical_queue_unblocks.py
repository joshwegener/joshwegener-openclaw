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
        self.col_done = 16

        self.task = {
            "id": 33,
            "title": "Server: Implement /v1/recall MVP retrieval",
            "description": f"Repo: {repo_path}\n",
            "column_id": self.col_backlog,
            "swimlane_id": 1,
            "position": 1,
        }
        # Simulate a queued-critical task from older runs: it has both `hold` and `hold:queued-critical`.
        self.tags_by_task_id = {33: ["critical", "hold", "hold:queued-critical", "story"]}
        self.moves = []  # (task_id, column_id)

    def rpc(self, method, params=None):
        if method == "getProjectByName":
            return {"id": self.pid}
        if method == "getBoard":
            return [
                {
                    "id": 1,
                    "name": "Default swimlane",
                    "columns": [
                        {"id": self.col_backlog, "title": "Backlog", "tasks": [self._task_card()]},
                        {"id": self.col_ready, "title": "Ready", "tasks": []},
                        {"id": self.col_wip, "title": "Work in progress", "tasks": []},
                        {"id": self.col_review, "title": "Review", "tasks": []},
                        {"id": self.col_blocked, "title": "Blocked", "tasks": []},
                        {"id": self.col_done, "title": "Done", "tasks": []},
                    ],
                }
            ]
        if method == "getTask":
            task_id = int(params[0])
            if task_id != 33:
                raise KeyError(task_id)
            return dict(self.task)
        if method == "getTaskTags":
            task_id = int(params.get("task_id"))
            tags = self.tags_by_task_id.get(task_id, [])
            return {str(i + 1): t for i, t in enumerate(tags)}
        if method == "setTaskTags":
            _pid, task_id, tags = params
            self.tags_by_task_id[int(task_id)] = list(tags)
            return True
        if method == "moveTaskPosition":
            task_id = int(params["task_id"])
            col_id = int(params["column_id"])
            if task_id == 33:
                self.task["column_id"] = col_id
            self.moves.append((task_id, col_id))
            return True

        raise NotImplementedError(method)

    def _task_card(self):
        return {"id": self.task["id"], "title": self.task["title"], "position": self.task["position"]}


class TestCriticalQueueUnblocks(unittest.TestCase):
    def test_queued_critical_is_unqueued_and_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            fake = FakeKanboard(repo_path=str(repo))

            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn_worker = bo.spawn_worker
            old_worker_spawn_cmd = bo.WORKER_SPAWN_CMD
            old_missing_worker_policy = bo.MISSING_WORKER_POLICY
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.MISSING_WORKER_POLICY = "pause"
                bo.WORKER_SPAWN_CMD = "dummy"

                def _spawn_worker(_task_id, _repo_key, _repo_path):
                    return {"execSessionId": "pid:1234", "logPath": "/tmp/x", "patchPath": "/tmp/p", "commentPath": "/tmp/c", "donePath": "/tmp/d", "startedAtMs": 1}

                bo.spawn_worker = _spawn_worker  # type: ignore[assignment]

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()

                self.assertEqual(rc, 0)
                payload = json.loads(buf.getvalue().strip())

                # The orchestrator should unqueue the active critical (remove hold tags),
                # then start it in WIP by spawning a worker.
                tags = {t.lower() for t in fake.tags_by_task_id[33]}
                self.assertNotIn("hold", tags)
                self.assertNotIn("hold:queued-critical", tags)
                self.assertEqual(fake.task["column_id"], fake.col_wip)
                self.assertTrue(any("Started critical" in a for a in payload.get("actions", [])))
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.spawn_worker = old_spawn_worker
                bo.WORKER_SPAWN_CMD = old_worker_spawn_cmd
                bo.MISSING_WORKER_POLICY = old_missing_worker_policy


if __name__ == "__main__":
    unittest.main()
