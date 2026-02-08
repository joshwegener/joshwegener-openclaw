import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import board_orchestrator as bo


class FakeKanboard:
    def __init__(self, *, tasks, tags_by_task_id=None):
        self.pid = 1

        # Column ids
        self.col_backlog = 10
        self.col_ready = 11
        self.col_wip = 12
        self.col_review = 13
        self.col_blocked = 14
        self.col_done = 16

        self.tasks = {int(t["id"]): dict(t) for t in tasks}
        self.tags_by_task_id = {int(k): list(v) for k, v in (tags_by_task_id or {}).items()}
        self.moves = []  # (task_id, column_id)
        self.comments = []  # (task_id, content)

    def _task_card(self, task_id: int):
        t = self.tasks[task_id]
        return {"id": t["id"], "title": t["title"], "position": t.get("position", 1)}

    def rpc(self, method, params=None):
        if method == "getProjectByName":
            return {"id": self.pid}
        if method == "getMe":
            return {"id": 1}
        if method == "getBoard":
            cols = [
                {"id": self.col_backlog, "title": "Backlog", "tasks": []},
                {"id": self.col_ready, "title": "Ready", "tasks": []},
                {"id": self.col_wip, "title": "Work in progress", "tasks": []},
                {"id": self.col_review, "title": "Review", "tasks": []},
                {"id": self.col_blocked, "title": "Blocked", "tasks": []},
                {"id": self.col_done, "title": "Done", "tasks": []},
            ]
            by_col = {c["id"]: c for c in cols}
            for tid, t in self.tasks.items():
                by_col[int(t["column_id"])]["tasks"].append(self._task_card(tid))
            return [{"id": 1, "name": "Default swimlane", "columns": cols}]
        if method == "getTask":
            task_id = int(params[0])
            if task_id not in self.tasks:
                raise KeyError(task_id)
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
            task_id = int(params["task_id"])
            col_id = int(params["column_id"])
            self.tasks[task_id]["column_id"] = col_id
            self.moves.append((task_id, col_id))
            return True
        if method == "createComment":
            tid = int(params["task_id"])
            self.comments.append((tid, str(params.get("content") or "")))
            return True
        raise NotImplementedError(method)


class TestRepoHoldSemantics(unittest.TestCase):
    def test_missing_repo_gets_hold_needs_repo_and_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard(
                tasks=[
                    {"id": 10, "title": "Some task", "description": "", "column_id": 10, "swimlane_id": 1, "position": 1}
                ],
                tags_by_task_id={10: ["story"]},
            )

            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()

                self.assertEqual(rc, 0)
                tags = {t.lower() for t in fake.tags_by_task_id[10]}
                self.assertIn("blocked:repo", tags)
                self.assertIn("hold:needs-repo", tags)
                self.assertIn("auto-blocked", tags)
                self.assertNotIn("no-repo", tags)
                self.assertEqual(len(fake.comments), 1)
                self.assertIn("explicit repo mapping", fake.comments[0][1].lower())
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock

    def test_plain_hold_is_normalized_to_hold_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard(
                tasks=[
                    {"id": 11, "title": "Held task", "description": "", "column_id": 10, "swimlane_id": 1, "position": 1}
                ],
                tags_by_task_id={11: ["hold"]},
            )

            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()

                self.assertEqual(rc, 0)
                tags = {t.lower() for t in fake.tags_by_task_id[11]}
                self.assertNotIn("hold", tags)
                self.assertIn("hold:manual", tags)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock

    def test_no_repo_tag_allows_worker_spawn_with_empty_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard(
                tasks=[
                    {"id": 12, "title": "No repo task", "description": "", "column_id": 11, "swimlane_id": 1, "position": 1}
                ],
                tags_by_task_id={12: ["no-repo"]},
            )

            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            captured = {"repo_path": None}

            def _spawn_worker(_task_id, _repo_key, repo_path):
                captured["repo_path"] = repo_path
                return {
                    "execSessionId": "pid:1234",
                    "logPath": "/tmp/x",
                    "patchPath": "/tmp/p",
                    "commentPath": "/tmp/c",
                    "donePath": "/tmp/d",
                    "startedAtMs": 1,
                }

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn_worker = bo.spawn_worker
            old_worker_spawn_cmd = bo.WORKER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.WORKER_SPAWN_CMD = "dummy"
                bo.spawn_worker = _spawn_worker  # type: ignore[assignment]

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()

                self.assertEqual(rc, 0)
                self.assertEqual(captured["repo_path"], "")
                self.assertEqual(fake.tasks[12]["column_id"], fake.col_wip)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.spawn_worker = old_spawn_worker
                bo.WORKER_SPAWN_CMD = old_worker_spawn_cmd


if __name__ == "__main__":
    unittest.main()

