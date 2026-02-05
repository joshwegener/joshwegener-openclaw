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
        self.comments_by_task_id: dict[int, list[str]] = {}

    def board(self):
        def card(task_id: int) -> dict:
            t = self.tasks[task_id]
            return {"id": t["id"], "title": t["title"], "position": t.get("position", 1)}

        cols = [
            {"id": self.col_backlog, "title": "Backlog", "tasks": []},
            {"id": self.col_ready, "title": "Ready", "tasks": []},
            {"id": self.col_wip, "title": "Work in progress", "tasks": []},
            {"id": self.col_review, "title": "Review", "tasks": []},
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
            if isinstance(params, dict):
                task_id = int(params.get("task_id"))
                column_id = int(params.get("column_id"))
                position = int(params.get("position") or 1)
            else:
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
            task_id = int(params.get("task_id"))
            content = str(params.get("content") or "")
            self.comments_by_task_id.setdefault(task_id, []).append(content)
            return True
        raise NotImplementedError(method)


class TestDocsWorkerAutomation(unittest.TestCase):
    def test_docs_pending_spawns_docs_worker_and_tags_inflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard()
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"dryRun": False, "dryRunRunsRemaining": 0}))

            source_repo = Path(tmp) / "source-repo"
            source_repo.mkdir(parents=True, exist_ok=True)

            patch_path = Path(tmp) / "source.patch"
            patch_path.write_text("diff --git a/a b/a\n+hello\n")

            tid = 1
            fake.tasks[tid] = {
                "id": tid,
                "title": "Docs: update docs",
                "description": f"Repo: {source_repo}",
                "column_id": fake.col_docs,
                "swimlane_id": 1,
                "position": 1,
            }
            fake.tags_by_task_id[tid] = ["docs:auto", "docs:pending"]

            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "workersByTaskId": {str(tid): {"patchPath": str(patch_path)}},
                    }
                )
            )

            called: dict[str, int] = {"count": 0}

            def fake_spawn_docs_worker(task_id, source_repo_key, source_repo_path, source_patch_path):
                called["count"] += 1
                return {
                    "kind": "docs",
                    "execSessionId": "opaque-handle",
                    "logPath": str(Path(tmp) / "docs.log"),
                    "runId": "run-1",
                    "runDir": str(Path(tmp) / "docs-run"),
                    "donePath": str(Path(tmp) / "done.json"),
                    "patchPath": str(Path(tmp) / "patch.patch"),
                    "commentPath": str(Path(tmp) / "kanboard-comment.md"),
                    "startedAtMs": 123,
                    "sourceRepoKey": source_repo_key or "",
                    "sourceRepoPath": source_repo_path or "",
                    "sourcePatchPath": source_patch_path or "",
                }

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_docs_spawn_cmd = bo.DOCS_SPAWN_CMD
            old_spawn_docs_worker = bo.spawn_docs_worker
            old_worker_spawn = bo.WORKER_SPAWN_CMD
            old_reviewer_spawn = bo.REVIEWER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.DOCS_SPAWN_CMD = "stub"
                bo.spawn_docs_worker = fake_spawn_docs_worker  # type: ignore[assignment]
                bo.WORKER_SPAWN_CMD = ""
                bo.REVIEWER_SPAWN_CMD = ""

                rc = bo.main()
                self.assertEqual(rc, 0)
                self.assertEqual(called["count"], 1)

                tags = {t.lower() for t in fake.tags_by_task_id.get(tid, [])}
                self.assertIn("docs:auto", tags)
                self.assertIn("docs:inflight", tags)
                self.assertNotIn("docs:pending", tags)

                saved = json.loads(state_path.read_text())
                self.assertIn("docsWorkersByTaskId", saved)
                self.assertIn(str(tid), saved["docsWorkersByTaskId"])
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.DOCS_SPAWN_CMD = old_docs_spawn_cmd
                bo.spawn_docs_worker = old_spawn_docs_worker
                bo.WORKER_SPAWN_CMD = old_worker_spawn
                bo.REVIEWER_SPAWN_CMD = old_reviewer_spawn

    def test_docs_done_empty_patch_marks_skip_and_moves_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard()
            state_path = Path(tmp) / "state.json"

            tid = 2
            fake.tasks[tid] = {
                "id": tid,
                "title": "Docs: no change needed",
                "description": "Repo: /tmp",
                "column_id": fake.col_docs,
                "swimlane_id": 1,
                "position": 1,
            }
            fake.tags_by_task_id[tid] = ["docs:auto", "docs:pending"]

            patch_path = Path(tmp) / "patch.patch"
            patch_path.write_text("")
            comment_path = Path(tmp) / "kanboard-comment.md"
            comment_path.write_text("Docs not needed for this change.")
            done_path = Path(tmp) / "done.json"
            done_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "taskId": tid,
                        "runId": "r1",
                        "ok": True,
                        "exitCode": 0,
                        "patchPath": str(patch_path),
                        "commentPath": str(comment_path),
                        "patchExists": True,
                        "commentExists": True,
                        "patchBytes": 0,
                        "commentBytes": len(comment_path.read_bytes()),
                    }
                )
            )

            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "docsWorkersByTaskId": {
                            str(tid): {
                                "execSessionId": "opaque-handle",
                                "donePath": str(done_path),
                                "patchPath": str(patch_path),
                                "commentPath": str(comment_path),
                                "runDir": str(Path(tmp)),
                                "logPath": str(Path(tmp) / "docs.log"),
                                "startedAtMs": 1,
                            }
                        },
                    }
                )
            )

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_docs_spawn_cmd = bo.DOCS_SPAWN_CMD
            old_worker_spawn = bo.WORKER_SPAWN_CMD
            old_reviewer_spawn = bo.REVIEWER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.DOCS_SPAWN_CMD = ""
                bo.WORKER_SPAWN_CMD = ""
                bo.REVIEWER_SPAWN_CMD = ""

                rc = bo.main()
                self.assertEqual(rc, 0)
                self.assertEqual(int(fake.tasks[tid]["column_id"]), fake.col_done)

                tags = {t.lower() for t in fake.tags_by_task_id.get(tid, [])}
                self.assertIn("docs:skip", tags)
                self.assertNotIn("docs:pending", tags)
                self.assertNotIn("docs:inflight", tags)

                self.assertEqual(fake.comments_by_task_id.get(tid), ["Docs not needed for this change."])

                saved = json.loads(state_path.read_text())
                self.assertNotIn(str(tid), (saved.get("docsWorkersByTaskId") or {}))
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.DOCS_SPAWN_CMD = old_docs_spawn_cmd
                bo.WORKER_SPAWN_CMD = old_worker_spawn
                bo.REVIEWER_SPAWN_CMD = old_reviewer_spawn

    def test_docs_done_empty_comment_tags_error_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard()
            state_path = Path(tmp) / "state.json"

            tid = 22
            fake.tasks[tid] = {
                "id": tid,
                "title": "Docs: missing comment",
                "description": "Repo: /tmp",
                "column_id": fake.col_docs,
                "swimlane_id": 1,
                "position": 1,
            }
            fake.tags_by_task_id[tid] = ["docs:auto", "docs:pending"]

            patch_path = Path(tmp) / "patch.patch"
            patch_path.write_text("")
            comment_path = Path(tmp) / "kanboard-comment.md"
            comment_path.write_text("")
            done_path = Path(tmp) / "done.json"
            done_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "taskId": tid,
                        "runId": "r1",
                        "ok": True,
                        "exitCode": 0,
                        "patchPath": str(patch_path),
                        "commentPath": str(comment_path),
                        "patchExists": True,
                        "commentExists": True,
                        "patchBytes": 0,
                        "commentBytes": 0,
                    }
                )
            )

            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "docsWorkersByTaskId": {
                            str(tid): {
                                "execSessionId": "opaque-handle",
                                "donePath": str(done_path),
                                "patchPath": str(patch_path),
                                "commentPath": str(comment_path),
                                "runDir": str(Path(tmp)),
                                "logPath": str(Path(tmp) / "docs.log"),
                                "startedAtMs": 1,
                            }
                        },
                    }
                )
            )

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_docs_spawn_cmd = bo.DOCS_SPAWN_CMD
            old_worker_spawn = bo.WORKER_SPAWN_CMD
            old_reviewer_spawn = bo.REVIEWER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.DOCS_SPAWN_CMD = ""
                bo.WORKER_SPAWN_CMD = ""
                bo.REVIEWER_SPAWN_CMD = ""

                rc = bo.main()
                self.assertEqual(rc, 0)
                self.assertEqual(int(fake.tasks[tid]["column_id"]), fake.col_docs)

                tags = {t.lower() for t in fake.tags_by_task_id.get(tid, [])}
                self.assertIn("docs:error", tags)
                self.assertNotIn("docs:pending", tags)

                self.assertTrue(fake.comments_by_task_id.get(tid))

                saved = json.loads(state_path.read_text())
                self.assertNotIn(str(tid), (saved.get("docsWorkersByTaskId") or {}))
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.DOCS_SPAWN_CMD = old_docs_spawn_cmd
                bo.WORKER_SPAWN_CMD = old_worker_spawn
                bo.REVIEWER_SPAWN_CMD = old_reviewer_spawn

    def test_docs_error_stops_respawn_until_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeKanboard()
            state_path = Path(tmp) / "state.json"

            tid = 3
            fake.tasks[tid] = {
                "id": tid,
                "title": "Docs: broken run",
                "description": "Repo: /tmp",
                "column_id": fake.col_docs,
                "swimlane_id": 1,
                "position": 1,
            }
            fake.tags_by_task_id[tid] = ["docs:auto", "docs:pending"]

            patch_path = Path(tmp) / "patch.patch"
            patch_path.write_text("")
            comment_path = Path(tmp) / "kanboard-comment.md"
            comment_path.write_text("failed")
            done_path = Path(tmp) / "done.json"
            done_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "taskId": tid,
                        "runId": "r1",
                        "ok": False,
                        "exitCode": 1,
                        "patchPath": str(patch_path),
                        "commentPath": str(comment_path),
                        "patchExists": True,
                        "commentExists": True,
                        "patchBytes": 0,
                        "commentBytes": len(comment_path.read_bytes()),
                    }
                )
            )

            state_path.write_text(
                json.dumps(
                    {
                        "dryRun": False,
                        "dryRunRunsRemaining": 0,
                        "docsWorkersByTaskId": {
                            str(tid): {
                                "execSessionId": "opaque-handle",
                                "donePath": str(done_path),
                                "patchPath": str(patch_path),
                                "commentPath": str(comment_path),
                                "runDir": str(Path(tmp)),
                                "logPath": str(Path(tmp) / "docs.log"),
                                "startedAtMs": 1,
                            }
                        },
                    }
                )
            )

            called: dict[str, int] = {"count": 0}

            def fake_spawn_docs_worker(*_args, **_kwargs):
                called["count"] += 1
                return None

            old_rpc = bo.rpc
            old_state = bo.STATE_PATH
            old_lock = bo.LOCK_PATH
            old_docs_spawn_cmd = bo.DOCS_SPAWN_CMD
            old_spawn_docs_worker = bo.spawn_docs_worker
            old_worker_spawn = bo.WORKER_SPAWN_CMD
            old_reviewer_spawn = bo.REVIEWER_SPAWN_CMD
            try:
                bo.rpc = fake.rpc  # type: ignore[assignment]
                bo.STATE_PATH = str(state_path)
                bo.LOCK_PATH = str(Path(tmp) / "lock.json")
                bo.DOCS_SPAWN_CMD = "stub"
                bo.spawn_docs_worker = fake_spawn_docs_worker  # type: ignore[assignment]
                bo.WORKER_SPAWN_CMD = ""
                bo.REVIEWER_SPAWN_CMD = ""

                rc = bo.main()
                self.assertEqual(rc, 0)
                tags = {t.lower() for t in fake.tags_by_task_id.get(tid, [])}
                self.assertIn("docs:error", tags)
                self.assertNotIn("docs:pending", tags)

                # Second tick should NOT respawn docs while docs:error remains.
                rc2 = bo.main()
                self.assertEqual(rc2, 0)
                self.assertEqual(called["count"], 0)
            finally:
                bo.rpc = old_rpc
                bo.STATE_PATH = old_state
                bo.LOCK_PATH = old_lock
                bo.DOCS_SPAWN_CMD = old_docs_spawn_cmd
                bo.spawn_docs_worker = old_spawn_docs_worker
                bo.WORKER_SPAWN_CMD = old_worker_spawn
                bo.REVIEWER_SPAWN_CMD = old_reviewer_spawn


if __name__ == "__main__":
    unittest.main()
