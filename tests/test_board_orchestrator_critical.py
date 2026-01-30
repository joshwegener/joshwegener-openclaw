import unittest
import json
import tempfile
from pathlib import Path

from scripts import board_orchestrator as bo


class TestCriticalHelpers(unittest.TestCase):
    def test_plan_pause_wip_skips_critical_and_already_paused(self) -> None:
        wip_task_ids = [1, 2, 3]
        critical_task_ids = {2}
        paused_by_critical = {"3": {"pausedAtMs": 10}}

        pause_ids = bo.plan_pause_wip(wip_task_ids, critical_task_ids, paused_by_critical)

        self.assertEqual(pause_ids, [1])

    def test_plan_resume_from_state_orders_and_respects_wip_limit(self) -> None:
        paused_by_critical = {
            "2": {"pausedAtMs": 200},
            "1": {"pausedAtMs": 100},
            "3": {"pausedAtMs": 200},
        }
        paused_task_ids = {1, 2, 3}

        resume_to_wip, resume_to_ready, drop_ids = bo.plan_resume_from_state(
            paused_by_critical, paused_task_ids, wip_count=1, wip_limit=2
        )

        self.assertEqual(resume_to_wip, [1])
        self.assertEqual(resume_to_ready, [2, 3])
        self.assertEqual(drop_ids, [])

    def test_plan_resume_from_state_drops_missing_tasks(self) -> None:
        paused_by_critical = {
            "1": {"pausedAtMs": 100},
            "2": {"pausedAtMs": 200},
        }
        paused_task_ids = {2}

        resume_to_wip, resume_to_ready, drop_ids = bo.plan_resume_from_state(
            paused_by_critical, paused_task_ids, wip_count=0, wip_limit=1
        )

        self.assertEqual(resume_to_wip, [2])
        self.assertEqual(resume_to_ready, [])
        self.assertEqual(drop_ids, [1])

    def test_critical_column_priority_prefers_wip_then_review_then_ready(self) -> None:
        col_wip = 10
        col_review = 11
        col_ready = 12
        col_other = 99

        self.assertLess(
            bo.critical_column_priority(col_wip, col_wip, col_review, col_ready),
            bo.critical_column_priority(col_ready, col_wip, col_review, col_ready),
        )
        self.assertLess(
            bo.critical_column_priority(col_review, col_wip, col_review, col_ready),
            bo.critical_column_priority(col_other, col_wip, col_review, col_ready),
        )


class TestRepoMappingHelpers(unittest.TestCase):
    def test_normalize_repo_key(self) -> None:
        self.assertEqual(bo.normalize_repo_key("RecallDeck-Server"), "recalldeck-server")
        self.assertEqual(bo.normalize_repo_key(" server "), "server")
        self.assertEqual(bo.normalize_repo_key("Server/API"), "server-api")

    def test_discover_repo_map_adds_recalldeck_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "RecallDeck-Server").mkdir()
            (root / "RecallDeck-Web").mkdir()

            m = bo.discover_repo_map(str(root))

            self.assertIn("recalldeck-server", m)
            self.assertIn("server", m)
            self.assertIn("api", m)
            self.assertIn("recalldeck-web", m)
            self.assertIn("web", m)

    def test_load_repo_map_from_file_normalizes_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "RecallDeck-Server"
            server.mkdir()
            p = root / "map.json"
            p.write_text(json.dumps({"Server": str(server)}))

            m = bo.load_repo_map_from_file(str(p))
            self.assertEqual(m["server"], str(server))

    def test_parse_repo_hint_precedence(self) -> None:
        tags = ["repo:server"]
        desc = "Repo: web"
        title = "docs: something"
        self.assertEqual(bo.parse_repo_hint(tags, desc, title), "server")

    def test_parse_repo_hint_with_source(self) -> None:
        hint, source = bo.parse_repo_hint_with_source(["repo:server"], "Repo: web", "docs: something")
        self.assertEqual(hint, "server")
        self.assertEqual(source, "tag")

        hint, source = bo.parse_repo_hint_with_source([], "Repo: web", "docs: something")
        self.assertEqual(hint, "web")
        self.assertEqual(source, "description")

        hint, source = bo.parse_repo_hint_with_source([], "", "server: something", allow_title_prefix=True)
        self.assertEqual(hint, "server")
        self.assertEqual(source, "title")

        hint, source = bo.parse_repo_hint_with_source([], "", "server: something", allow_title_prefix=False)
        self.assertIsNone(hint)
        self.assertIsNone(source)

    def test_resolve_repo_path_direct_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "MyRepo"
            repo.mkdir()
            key, path = bo.resolve_repo_path(str(repo), {})
            self.assertEqual(key, "myrepo")
            self.assertEqual(path, str(repo))

    def test_merge_repo_maps_prunes_non_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "RecallDeck-Server"
            server.mkdir()
            merged = bo.merge_repo_maps({"server": "/does/not/exist"}, {"server": str(server)})
            self.assertEqual(merged["server"], str(server))


if __name__ == "__main__":
    unittest.main()
