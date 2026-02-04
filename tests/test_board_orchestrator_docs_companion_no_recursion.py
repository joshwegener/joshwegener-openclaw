import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import board_orchestrator as bo


class FakeKanboard:
    def __init__(self) -> None:
        self.pid = 1

        # Column ids
        self.col_backlog = 10
        self.col_ready = 11
        self.col_wip = 12
        self.col_review = 13
        self.col_blocked = 14
        self.col_paused = 15
        self.col_done = 16

        self.task_id = 100
        self.task = {
            "id": self.task_id,
            "title": "Docs: (from #1) Server: POST /v1/memories",
            "description": "Docs companion task auto-created for review card #1.\n\nSource: #1 Server: POST /v1/memories\n",
            "column_id": self.col_review,
            "swimlane_id": 1,
            "position": 1,
        }
        self.tags_by_task_id = {self.task_id: ["docs", "docs-required", "review:auto"]}

        self.created: list[tuple[str, str]] = []  # (title, desc)

    def rpc(self, method, params=None):
        if method == "getProjectByName":
            return {"id": self.pid}
        if method == "getBoard":
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
            # Not needed for this test.
            return True
        if method == "getMe":
            return {"id": 1}
        if method == "createComment":
            return True
        if method == "createTask":
            title = str(params.get("title") or "")
            desc = str(params.get("description") or "")
            self.created.append((title, desc))
            # Return a fake new task id
            return 999
        raise NotImplementedError(method)

    def _task_card(self):
        return {"id": self.task["id"], "title": self.task["title"], "position": self.task["position"]}


class TestDocsCompanionNoRecursion(unittest.TestCase):
    def test_docs_companion_in_review_does_not_spawn_docs_companion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard()
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_reviewer_spawn = bo.REVIEWER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.REVIEWER_SPAWN_CMD = ""  # avoid spawning a reviewer; irrelevant to this test

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()
                self.assertEqual(rc, 0)

                # Regression guard: we should NOT create docs tasks for docs tasks.
                self.assertEqual(fake.created, [])
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.REVIEWER_SPAWN_CMD = old_reviewer_spawn


if __name__ == "__main__":
    unittest.main()

