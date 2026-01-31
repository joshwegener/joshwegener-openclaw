import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts import critical_monitor as cm


class FakeKanboard:
    def __init__(self, *, paused_noncritical: bool):
        self.pid = 1
        # Column ids
        self.cols = {
            10: "Backlog",
            11: "Ready",
            12: "Work in progress",
            13: "Review",
            14: "Done",
        }
        self.paused_noncritical = paused_noncritical

    def rpc(self, method, params=None):
        if method == "getProjectByName":
            return {"id": self.pid}
        if method == "getColumns":
            return [{"id": str(k), "title": v} for k, v in self.cols.items()]
        if method == "getActiveSwimlanes":
            return [{"id": "1", "name": "Default swimlane", "position": 1}]
        if method == "getAllTasks":
            # Two critical tasks + one noncritical in WIP.
            return [
                {"id": "59", "title": "critical top", "column_id": "12", "swimlane_id": "1", "position": "1"},
                {"id": "60", "title": "critical queued", "column_id": "10", "swimlane_id": "1", "position": "2"},
                {"id": "10", "title": "noncritical wip", "column_id": "12", "swimlane_id": "1", "position": "3"},
            ]
        if method == "getTaskTags":
            tid = int(params.get("task_id"))
            if tid == 59:
                return {"1": "critical"}
            if tid == 60:
                return {"1": "critical"}
            if tid == 10:
                if self.paused_noncritical:
                    return {"1": "paused:critical"}
                return {}
            return {}
        raise NotImplementedError(method)


class TestCriticalMonitorMultipleCritical(unittest.TestCase):
    def test_multiple_critical_does_not_trigger_multiple_critical_alert(self) -> None:
        # When multiple critical tasks exist, the monitor should focus on the top-priority one,
        # and alert only on real drift (here: a non-critical, non-paused task in WIP).
        fake = FakeKanboard(paused_noncritical=False)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"swimlanePriority": ["Default swimlane"], "workersByTaskId": {}}))

            old_rpc = cm.rpc
            old_state = cm.STATE_PATH
            try:
                cm.rpc = fake.rpc  # type: ignore[assignment]
                cm.STATE_PATH = str(state_path)

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = cm.main()

                self.assertEqual(rc, 0)
                out = buf.getvalue()
                self.assertIn("ALERT:", out)
                self.assertIn("Non-critical tasks still in WIP", out)
                self.assertNotIn("Multiple critical tasks present", out)
            finally:
                cm.rpc = old_rpc
                cm.STATE_PATH = old_state

    def test_paused_noncritical_wip_is_allowed_during_critical(self) -> None:
        fake = FakeKanboard(paused_noncritical=True)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps({"swimlanePriority": ["Default swimlane"], "workersByTaskId": {"59": {"execSessionId": "pid:1"}}})
            )

            old_rpc = cm.rpc
            old_state = cm.STATE_PATH
            try:
                cm.rpc = fake.rpc  # type: ignore[assignment]
                cm.STATE_PATH = str(state_path)

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = cm.main()

                self.assertEqual(rc, 0)
                out = buf.getvalue().strip()
                self.assertTrue(out == "NO_REPLY" or out.startswith("STATUS:"))
            finally:
                cm.rpc = old_rpc
                cm.STATE_PATH = old_state


if __name__ == "__main__":
    unittest.main()
