import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import board_orchestrator as bo


class FakeKanboard:
    def __init__(self, tasks: dict[int, dict], tags_by_task_id: dict[int, list[str]]):
        self.pid = 1
        self.tasks = {int(k): dict(v) for k, v in tasks.items()}
        self.tags_by_task_id = {int(k): list(v) for k, v in tags_by_task_id.items()}
        self.moves: list[tuple[int, int]] = []

        # Column ids
        self.col_backlog = 10
        self.col_ready = 11
        self.col_wip = 12
        self.col_review = 13
        self.col_blocked = 14
        self.col_paused = 15
        self.col_done = 16

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


class TestDeadlockRecovery(unittest.TestCase):
    def test_critical_in_review_does_not_freeze_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            critical_id = 200
            paused_wip_id = 201
            ready_id = 202

            fake = FakeKanboard(
                tasks={
                    critical_id: {
                        "id": critical_id,
                        "title": "CRITICAL: fix deadlock",
                        "description": f"Repo: {repo}\n",
                        "column_id": 13,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                    paused_wip_id: {
                        "id": paused_wip_id,
                        "title": "Normal WIP (paused by critical)",
                        "description": f"Repo: {repo}\n",
                        "column_id": 12,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                    ready_id: {
                        "id": ready_id,
                        "title": "Normal Ready work",
                        "description": f"Repo: {repo}\n",
                        "column_id": 11,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                },
                tags_by_task_id={
                    critical_id: ["critical"],
                    paused_wip_id: ["paused", "paused:critical"],
                    ready_id: [],
                },
            )

            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "pausedByCritical": {
                            str(paused_wip_id): {
                                "criticalTaskId": critical_id,
                                "pausedAtMs": 1,
                                "swimlaneId": 1,
                                "addedPaused": True,
                            }
                        },
                        "workersByTaskId": {
                            str(paused_wip_id): {"execSessionId": "opaque-worker", "logPath": str(Path(tmp) / "w.log")}
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
                bo.WORKER_SPAWN_CMD = "echo opaque-worker"
                bo.WORKER_LEASES_ENABLED = False

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()
                self.assertEqual(rc, 0)

                payload = json.loads(buf.getvalue().strip())
                self.assertTrue(any("Cleared paused:critical" in a for a in payload.get("actions", [])))
                self.assertTrue(any(f"Moved Ready #{ready_id}" in a for a in payload.get("actions", [])))

                tags = {t.lower() for t in fake.tags_by_task_id[paused_wip_id]}
                self.assertNotIn("paused:critical", tags)
                self.assertNotIn("paused", tags)
                self.assertEqual(fake.tasks[ready_id]["column_id"], fake.col_wip)

                st = json.loads(state_path.read_text())
                self.assertNotIn(str(paused_wip_id), st.get("pausedByCritical") or {})
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.WORKER_SPAWN_CMD = old_spawn
                bo.WORKER_LEASES_ENABLED = old_leases

    def test_paused_wip_does_not_consume_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            paused_wip_id = 300
            ready_id = 301

            fake = FakeKanboard(
                tasks={
                    paused_wip_id: {
                        "id": paused_wip_id,
                        "title": "WIP paused (missing worker)",
                        "description": f"Repo: {repo}\n",
                        "column_id": 12,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                    ready_id: {
                        "id": ready_id,
                        "title": "Ready work should still start",
                        "description": f"Repo: {repo}\n",
                        "column_id": 11,
                        "swimlane_id": 1,
                        "position": 1,
                    },
                },
                tags_by_task_id={
                    paused_wip_id: ["paused", "paused:missing-worker"],
                    ready_id: [],
                },
            )

            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_spawn = bo.WORKER_SPAWN_CMD
            old_limit = bo.WIP_LIMIT
            old_leases = bo.WORKER_LEASES_ENABLED
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.WORKER_SPAWN_CMD = "echo opaque-worker"
                bo.WIP_LIMIT = 1
                bo.WORKER_LEASES_ENABLED = False

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()
                self.assertEqual(rc, 0)

                payload = json.loads(buf.getvalue().strip())
                self.assertTrue(any(f"Moved Ready #{ready_id}" in a for a in payload.get("actions", [])))
                self.assertEqual(fake.tasks[ready_id]["column_id"], fake.col_wip)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.WORKER_SPAWN_CMD = old_spawn
                bo.WIP_LIMIT = old_limit
                bo.WORKER_LEASES_ENABLED = old_leases

    def test_stale_worker_watchdog_pauses_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            stale_wip_id = 400
            log_path = Path(tmp) / "stale-worker.log"
            log_path.write_text("still running\n")
            past = int((os.path.getmtime(log_path) - 10))
            os.utime(log_path, (past, past))

            fake = FakeKanboard(
                tasks={
                    stale_wip_id: {
                        "id": stale_wip_id,
                        "title": "WIP with stale worker log",
                        "description": f"Repo: {repo}\n",
                        "column_id": 12,
                        "swimlane_id": 1,
                        "position": 1,
                    }
                },
                tags_by_task_id={stale_wip_id: []},
            )

            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_root = bo.WORKER_LEASE_ROOT
            old_leases = bo.WORKER_LEASES_ENABLED
            old_stale_ms = bo.WORKER_LOG_STALE_MS
            old_action = bo.WORKER_LOG_STALE_ACTION
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.WORKER_LEASE_ROOT = str(Path(tmp) / "leases")
                bo.WORKER_LEASES_ENABLED = True
                bo.WORKER_LOG_STALE_MS = 1  # ms
                bo.WORKER_LOG_STALE_ACTION = "pause"

                lease = bo.init_lease_payload(
                    stale_wip_id,
                    "run-1",
                    "repo",
                    str(repo),
                    str(log_path),
                    str(Path(tmp) / "p.patch"),
                    str(Path(tmp) / "c.md"),
                    "spawn-cmd",
                    2,
                )
                lease["worker"]["pid"] = os.getpid()
                lease["worker"]["logPath"] = str(log_path)
                bo.ensure_dir(os.path.dirname(bo.lease_json_path(stale_wip_id)))
                Path(bo.lease_json_path(stale_wip_id)).write_text(json.dumps(lease))

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = bo.main()
                self.assertEqual(rc, 0)

                payload = json.loads(buf.getvalue().strip())
                self.assertTrue(any("paused:stale-worker" in a for a in payload.get("actions", [])))
                tags = {t.lower() for t in fake.tags_by_task_id[stale_wip_id]}
                self.assertIn("paused", tags)
                self.assertIn("paused:stale-worker", tags)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.WORKER_LEASE_ROOT = old_root
                bo.WORKER_LEASES_ENABLED = old_leases
                bo.WORKER_LOG_STALE_MS = old_stale_ms
                bo.WORKER_LOG_STALE_ACTION = old_action


if __name__ == "__main__":
    unittest.main()

