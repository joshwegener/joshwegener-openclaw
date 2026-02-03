import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import board_orchestrator as bo


class FakeKanboard:
    def __init__(self, *, tasks: dict[int, dict], tags_by_task_id: dict[int, list[str]]):
        self.pid = 1
        self.tasks = {int(k): dict(v) for k, v in tasks.items()}
        self.tags_by_task_id = {int(k): list(v) for k, v in tags_by_task_id.items()}

        # Column ids
        self.col_backlog = 10
        self.col_ready = 11
        self.col_wip = 12
        self.col_review = 13
        self.col_blocked = 14
        self.col_paused = 15
        self.col_done = 16

        self.moves: list[tuple[int, int]] = []

    def rpc(self, method, params=None):
        if method == "getProjectByName":
            return {"id": self.pid}
        if method == "getBoard":
            return [
                {
                    "id": 1,
                    "name": "Default swimlane",
                    "columns": [
                        {"id": self.col_backlog, "title": "Backlog", "tasks": self._cards(self.col_backlog)},
                        {"id": self.col_ready, "title": "Ready", "tasks": self._cards(self.col_ready)},
                        {"id": self.col_wip, "title": "Work in progress", "tasks": self._cards(self.col_wip)},
                        {"id": self.col_review, "title": "Review", "tasks": self._cards(self.col_review)},
                        {"id": self.col_blocked, "title": "Blocked", "tasks": self._cards(self.col_blocked)},
                        {"id": self.col_paused, "title": "Paused", "tasks": self._cards(self.col_paused)},
                        {"id": self.col_done, "title": "Done", "tasks": self._cards(self.col_done)},
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
            self.moves.append((task_id, col_id))
            return True
        if method == "getMe":
            return {"id": 1}
        if method == "createComment":
            return True
        raise NotImplementedError(method)

    def _cards(self, col_id: int) -> list[dict]:
        cards: list[dict] = []
        for t in self.tasks.values():
            if int(t.get("column_id") or 0) != int(col_id):
                continue
            cards.append({"id": t["id"], "title": t["title"], "position": int(t.get("position") or 1)})
        return sorted(cards, key=lambda c: int(c.get("position") or 10**9))


class TestCooldown(unittest.TestCase):
    def test_backlog_promotion_respects_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            log1 = Path(tmp) / "w1.log"
            log2 = Path(tmp) / "w2.log"
            log1.write_text("still running\n")
            log2.write_text("still running\n")

            t1 = 1
            t2 = 2
            w1 = 101
            w2 = 102

            fake = FakeKanboard(
                tasks={
                    # Fill WIP so we only test Backlog -> Ready behavior.
                    w1: {
                        "id": w1,
                        "title": "WIP 1",
                        "description": f"Repo: {repo}\n",
                        "column_id": 12,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                    w2: {
                        "id": w2,
                        "title": "WIP 2",
                        "description": f"Repo: {repo}\n",
                        "column_id": 12,
                        "swimlane_id": 1,
                        "position": 2,
                    },
                    t1: {
                        "id": t1,
                        "title": "Backlog top (in cooldown)",
                        "description": f"Repo: {repo}\n",
                        "column_id": 10,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                    t2: {
                        "id": t2,
                        "title": "Backlog next (eligible)",
                        "description": f"Repo: {repo}\n",
                        "column_id": 10,
                        "swimlane_id": 1,
                        "position": 2,
                    },
                },
                tags_by_task_id={
                    w1: [],
                    w2: [],
                    t1: [],
                    t2: [],
                },
            )

            state_path = Path(tmp) / "state.json"
            nowm = bo.now_ms()
            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "lastActionsByTaskId": {str(t1): nowm - 60_000},
                        "workersByTaskId": {
                            str(w1): {"execSessionId": "opaque-worker", "logPath": str(log1)},
                            str(w2): {"execSessionId": "opaque-worker", "logPath": str(log2)},
                        },
                    }
                )
            )

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn = bo.WORKER_SPAWN_CMD
            old_leases = bo.WORKER_LEASES_ENABLED
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.WORKER_SPAWN_CMD = ""  # irrelevant; WIP is full
                bo.WORKER_LEASES_ENABLED = False

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()
                self.assertEqual(rc, 0)

                # Top backlog item is in cooldown; next eligible should be promoted instead.
                self.assertEqual(fake.tasks[t1]["column_id"], fake.col_backlog)
                self.assertEqual(fake.tasks[t2]["column_id"], fake.col_ready)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.WORKER_SPAWN_CMD = old_spawn
                bo.WORKER_LEASES_ENABLED = old_leases

    def test_ready_autoblock_respects_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            log1 = Path(tmp) / "w1.log"
            log2 = Path(tmp) / "w2.log"
            log1.write_text("still running\n")
            log2.write_text("still running\n")

            ready_bad = 10
            ready_ok = 11
            w1 = 201
            w2 = 202

            fake = FakeKanboard(
                tasks={
                    w1: {
                        "id": w1,
                        "title": "WIP 1",
                        "description": f"Repo: {repo}\n",
                        "column_id": 12,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                    w2: {
                        "id": w2,
                        "title": "WIP 2",
                        "description": f"Repo: {repo}\n",
                        "column_id": 12,
                        "swimlane_id": 1,
                        "position": 2,
                    },
                    ready_bad: {
                        "id": ready_bad,
                        "title": "Ready but blocked by deps",
                        "description": f"Repo: {repo}\nDepends on: #999\n",
                        "column_id": 11,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                    ready_ok: {
                        "id": ready_ok,
                        "title": "Ready work",
                        "description": f"Repo: {repo}\n",
                        "column_id": 11,
                        "swimlane_id": 1,
                        "position": 2,
                    },
                    999: {
                        "id": 999,
                        "title": "Dep task",
                        "description": "",
                        "column_id": 10,
                        "swimlane_id": 1,
                        "position": 99,
                    },
                },
                tags_by_task_id={w1: [], w2: [], ready_bad: [], ready_ok: [], 999: []},
            )

            state_path = Path(tmp) / "state.json"
            nowm = bo.now_ms()
            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "lastActionsByTaskId": {str(ready_bad): nowm - 60_000},
                        "workersByTaskId": {
                            str(w1): {"execSessionId": "opaque-worker", "logPath": str(log1)},
                            str(w2): {"execSessionId": "opaque-worker", "logPath": str(log2)},
                        },
                    }
                )
            )

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn = bo.WORKER_SPAWN_CMD
            old_leases = bo.WORKER_LEASES_ENABLED
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.WORKER_SPAWN_CMD = ""  # irrelevant; WIP is full
                bo.WORKER_LEASES_ENABLED = False

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()
                self.assertEqual(rc, 0)

                # Within cooldown: do not auto-move Ready -> Blocked.
                self.assertEqual(fake.tasks[ready_bad]["column_id"], fake.col_ready)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.WORKER_SPAWN_CMD = old_spawn
                bo.WORKER_LEASES_ENABLED = old_leases

    def test_ready_to_wip_not_blocked_by_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            tid = 20
            fake = FakeKanboard(
                tasks={
                    tid: {
                        "id": tid,
                        "title": "Ready start",
                        "description": f"Repo: {repo}\n",
                        "column_id": 11,
                        "swimlane_id": 1,
                        "position": 1,
                    }
                },
                tags_by_task_id={tid: []},
            )

            state_path = Path(tmp) / "state.json"
            nowm = bo.now_ms()
            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "lastActionsByTaskId": {str(tid): nowm - 60_000},
                    }
                )
            )

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn = bo.WORKER_SPAWN_CMD
            old_leases = bo.WORKER_LEASES_ENABLED
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.WORKER_SPAWN_CMD = "echo opaque-worker"
                bo.WORKER_LEASES_ENABLED = False

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()
                self.assertEqual(rc, 0)

                self.assertEqual(fake.tasks[tid]["column_id"], fake.col_wip)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.WORKER_SPAWN_CMD = old_spawn
                bo.WORKER_LEASES_ENABLED = old_leases


if __name__ == "__main__":
    unittest.main()
