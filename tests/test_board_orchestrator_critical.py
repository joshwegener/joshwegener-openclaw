import unittest

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


if __name__ == "__main__":
    unittest.main()
