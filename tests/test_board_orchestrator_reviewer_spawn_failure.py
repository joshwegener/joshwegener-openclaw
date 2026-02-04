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

        self.task_id = 201
        self.task = {
            "id": self.task_id,
            "title": "Review spawn failure escalates to review:error",
            "description": f"Repo: {repo_path}\n",
            "column_id": self.col_review,
            "swimlane_id": 1,
            "position": 1,
        }
        self.tags_by_task_id = {self.task_id: ["review:auto"]}
        self.comments: list[tuple[int, str]] = []

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
                        {"id": self.col_ready, "title": "Ready", "tasks": []},
                        {"id": self.col_wip, "title": "Work in progress", "tasks": []},
                        {"id": self.col_review, "title": "Review", "tasks": [self._task_card()]},
                        {"id": self.col_blocked, "title": "Blocked", "tasks": []},
                        {"id": self.col_paused, "title": "Paused", "tasks": []},
                        {"id": self.col_done, "title": "Done", "tasks": []},
                    ],
                }
            ]
        if method == "getTask":
            task_id = int(params[0])
            if task_id != self.task_id:
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
            # No moves expected in this test.
            task_id = int(params["task_id"])
            col_id = int(params["column_id"])
            if task_id == self.task_id:
                self.task["column_id"] = col_id
            return True
        if method == "getMe":
            return {"id": 1}
        if method == "createComment":
            # params: {"task_id": ..., "user_id": ..., "comment": "..."}
            task_id = int(params.get("task_id") or 0)
            comment = str(params.get("comment") or "")
            self.comments.append((task_id, comment))
            return True
        raise NotImplementedError(method)

    def _task_card(self):
        return {"id": self.task["id"], "title": self.task["title"], "position": self.task["position"]}


class TestReviewerSpawnFailureEscalation(unittest.TestCase):
    def test_spawn_failure_escalates_to_review_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            fake = FakeKanboard(repo_path=str(repo))

            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn_cmd = bo.REVIEWER_SPAWN_CMD
            old_spawn_reviewer = bo.spawn_reviewer
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.REVIEWER_SPAWN_CMD = "dummy"  # enables spawn attempt path

                def _spawn_reviewer_fail(*_a, **_k):
                    return None

                bo.spawn_reviewer = _spawn_reviewer_fail  # type: ignore[assignment]

                # Run 3 ticks; the third should escalate to review:error.
                for i in range(3):
                    buf = StringIO()
                    with redirect_stdout(buf):
                        rc = bo.main()
                    self.assertEqual(rc, 0, f"tick {i} failed")

                tags = {t.lower() for t in fake.tags_by_task_id[fake.task_id]}
                self.assertIn("review:error", tags)
                self.assertNotIn("review:pending", tags)
                self.assertNotIn("review:inflight", tags)

                # We should have posted at least one escalation comment.
                self.assertTrue(any(tid == fake.task_id for tid, _c in fake.comments))

                # Counter should be persisted.
                state = json.loads(state_path.read_text())
                failures = state.get("reviewerSpawnFailuresByTaskId", {}).get(str(fake.task_id), {})
                self.assertIsInstance(failures, dict)
                self.assertGreaterEqual(int(failures.get("count") or 0), 3)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.REVIEWER_SPAWN_CMD = old_spawn_cmd
                bo.spawn_reviewer = old_spawn_reviewer


if __name__ == "__main__":
    unittest.main()

