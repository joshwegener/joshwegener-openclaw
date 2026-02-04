import hashlib
import json
import tempfile
import unittest
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
        self.col_docs = 14
        self.col_blocked = 15
        self.col_done = 16

        self.tasks: dict[int, dict] = {}
        self.tags_by_task_id: dict[int, list[str]] = {}

    def board(self):
        def card(task_id: int) -> dict:
            t = self.tasks[task_id]
            return {"id": t["id"], "title": t["title"], "position": t.get("position", 1)}

        cols = [
            {"id": self.col_backlog, "title": "Backlog", "tasks": []},
            {"id": self.col_ready, "title": "Ready", "tasks": []},
            {"id": self.col_wip, "title": "Work in progress", "tasks": []},
            {
                "id": self.col_review,
                "title": "Review",
                "tasks": [card(tid) for tid, t in self.tasks.items() if int(t["column_id"]) == self.col_review],
            },
            {
                "id": self.col_docs,
                "title": "Documentation",
                "tasks": [card(tid) for tid, t in self.tasks.items() if int(t["column_id"]) == self.col_docs],
            },
            {"id": self.col_blocked, "title": "Blocked", "tasks": []},
            {"id": self.col_done, "title": "Done", "tasks": []},
        ]

        return [{"id": 1, "name": "Default swimlane", "columns": cols}]

    def rpc(self, method, params=None):
        if method == "getProjectByName":
            return {"id": self.pid}
        if method == "getBoard":
            return self.board()
        if method == "getTask":
            task_id = int(params[0])
            return dict(self.tasks[task_id])
        if method == "getTaskTags":
            task_id = int(params.get("task_id"))
            tags = self.tags_by_task_id.get(task_id, [])
            return {str(i + 1): t for i, t in enumerate(tags)}
        if method == "setTaskTags":
            _pid, task_id, tags = params
            self.tags_by_task_id[int(task_id)] = list(tags)
            return True
        if method == "moveTaskPosition":
            # Accept both legacy list params and canonical dict params.
            if isinstance(params, dict):
                task_id = int(params.get("task_id"))
                column_id = int(params.get("column_id"))
                position = int(params.get("position") or 1)
            else:
                # params: project_id, task_id, column_id, position, swimlane_id
                _pid, task_id, column_id, position, _swimlane_id = params
                task_id = int(task_id)
                column_id = int(column_id)
                position = int(position)
            t = self.tasks[task_id]
            t["column_id"] = column_id
            t["position"] = position
            return True
        if method == "getMe":
            return {"id": 1}
        if method == "createComment":
            return True
        raise NotImplementedError(method)


class TestDocumentationFlow(unittest.TestCase):
    def test_review_pass_moves_to_documentation_and_tags_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard()
            state_path = Path(tmp) / "state.json"
            patch_path = Path(tmp) / "x.patch"
            patch_bytes = b"diff --git a/a b/a\n+hello\n"
            patch_path.write_bytes(patch_bytes)
            rev = hashlib.sha256(patch_bytes).hexdigest()

            # Task starts in Review.
            tid = 1
            fake.tasks[tid] = {
                "id": tid,
                "title": "Server: add docs flow",
                "description": "n/a",
                "column_id": fake.col_review,
                "swimlane_id": 1,
                "position": 1,
            }
            fake.tags_by_task_id[tid] = ["review:auto", "review:pending"]

            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "workersByTaskId": {str(tid): {"patchPath": str(patch_path)}},
                        "reviewResultsByTaskId": {
                            str(tid): {
                                "score": 95,
                                "verdict": "PASS",
                                "critical_items": [],
                                "reviewRevision": rev,
                            }
                        },
                    }
                )
            )

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_reviewer_spawn = bo.REVIEWER_SPAWN_CMD
            old_worker_spawn = bo.WORKER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.REVIEWER_SPAWN_CMD = ""
                bo.WORKER_SPAWN_CMD = ""

                rc = bo.main()
                self.assertEqual(rc, 0)

                # Review PASS should auto-advance to Documentation (not Done).
                self.assertEqual(int(fake.tasks[tid]["column_id"]), fake.col_docs)

                tags = {t.lower() for t in fake.tags_by_task_id.get(tid, [])}
                self.assertIn("review:pass", tags)
                self.assertIn("docs:pending", tags)
                self.assertIn("docs:auto", tags)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.REVIEWER_SPAWN_CMD = old_reviewer_spawn
                bo.WORKER_SPAWN_CMD = old_worker_spawn

    def test_documentation_completed_moves_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard()
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            tid = 2
            fake.tasks[tid] = {
                "id": tid,
                "title": "Docs done",
                "description": "n/a",
                "column_id": fake.col_docs,
                "swimlane_id": 1,
                "position": 1,
            }
            fake.tags_by_task_id[tid] = ["docs:completed", "docs:pending", "docs:auto"]

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_reviewer_spawn = bo.REVIEWER_SPAWN_CMD
            old_worker_spawn = bo.WORKER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.REVIEWER_SPAWN_CMD = ""
                bo.WORKER_SPAWN_CMD = ""

                rc = bo.main()
                self.assertEqual(rc, 0)

                self.assertEqual(int(fake.tasks[tid]["column_id"]), fake.col_done)
                tags = {t.lower() for t in fake.tags_by_task_id.get(tid, [])}
                self.assertIn("docs:completed", tags)
                self.assertNotIn("docs:pending", tags)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.REVIEWER_SPAWN_CMD = old_reviewer_spawn
                bo.WORKER_SPAWN_CMD = old_worker_spawn


if __name__ == "__main__":
    unittest.main()
