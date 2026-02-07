import json
import os
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

        self.ready_id = 101
        self.tasks = {
            self.ready_id: {
                "id": self.ready_id,
                "title": "Ready task should not spawn when provider is blocked",
                "description": f"Repo: {repo_path}\n",
                "column_id": self.col_ready,
                "swimlane_id": 1,
                "position": 1,
            }
        }
        self.tags_by_task_id: dict[int, list[str]] = {self.ready_id: []}

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
                        {"id": self.col_ready, "title": "Ready", "tasks": [self._card(self.ready_id)]},
                        {"id": self.col_wip, "title": "Work in progress", "tasks": []},
                        {"id": self.col_review, "title": "Review", "tasks": []},
                        {"id": self.col_blocked, "title": "Blocked", "tasks": []},
                        {"id": self.col_done, "title": "Done", "tasks": []},
                    ],
                }
            ]
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
            task_id = int(params["task_id"])
            col_id = int(params["column_id"])
            self.tasks[task_id]["column_id"] = col_id
            return True
        if method == "getMe":
            return {"id": 1}
        if method == "createComment":
            return True
        raise NotImplementedError(method)

    def _card(self, task_id: int) -> dict:
        t = self.tasks[task_id]
        return {"id": t["id"], "title": t["title"], "position": t["position"]}


class TestProviderPreflight(unittest.TestCase):
    def test_provider_auth_failure_blocks_spawn_and_tags_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            fake = FakeKanboard(repo_path=str(repo))

            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn_cmd = bo.WORKER_SPAWN_CMD
            old_preflight = bo.preflight_codex
            old_spawn_worker = bo.spawn_worker
            old_enabled = bo.PREFLIGHT_ENABLED
            old_env = dict(os.environ)
            calls = {"preflight": 0, "spawn": 0}
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                # Heuristic should infer codex based on "spawn_worker" substring.
                bo.WORKER_SPAWN_CMD = "/Users/joshwegener/clawd/scripts/spawn_worker_tmux.sh {task_id} {repo_key} {repo_path}"
                bo.PREFLIGHT_ENABLED = True

                def _preflight_codex(*, timeout_sec: int):
                    calls["preflight"] += 1
                    return {"ok": False, "category": "auth", "message": "Not logged in"}

                def _spawn_worker(*_a, **_k):
                    calls["spawn"] += 1
                    return {"execSessionId": "pid:999", "logPath": str(Path(tmp) / "w.log")}

                bo.preflight_codex = _preflight_codex  # type: ignore[assignment]
                bo.spawn_worker = _spawn_worker  # type: ignore[assignment]

                buf1 = StringIO()
                with redirect_stdout(buf1):
                    rc = bo.main()
                self.assertEqual(rc, 0)

                payload1 = json.loads(buf1.getvalue().strip())
                self.assertTrue(any("provider codex unavailable" in e for e in payload1.get("errors", [])))

                tags = {t.lower() for t in fake.tags_by_task_id[fake.ready_id]}
                self.assertIn("blocked:auth", tags)
                self.assertIn("auto-blocked", tags)
                # Should not have moved to WIP.
                self.assertEqual(fake.tasks[fake.ready_id]["column_id"], fake.col_ready)
                # Spawn should not have been attempted.
                self.assertEqual(calls["spawn"], 0)
                self.assertEqual(calls["preflight"], 1)

                # Second tick should respect backoff and not re-run preflight.
                buf2 = StringIO()
                with redirect_stdout(buf2):
                    rc2 = bo.main()
                self.assertEqual(rc2, 0)
                self.assertEqual(calls["preflight"], 1)
                self.assertEqual(calls["spawn"], 0)
            finally:
                os.environ.clear()
                os.environ.update(old_env)
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.WORKER_SPAWN_CMD = old_spawn_cmd
                bo.preflight_codex = old_preflight
                bo.spawn_worker = old_spawn_worker
                bo.PREFLIGHT_ENABLED = old_enabled


if __name__ == "__main__":
    unittest.main()

