import unittest
from pathlib import Path


class TestTmuxSpawnGetTaskParams(unittest.TestCase):
    def test_spawn_worker_tmux_uses_positional_params(self) -> None:
        content = Path("scripts/spawn_worker_tmux.sh").read_text(encoding="utf-8")
        self.assertIn(
            'payload = {"jsonrpc": "2.0", "method": "getTask", "id": 1, "params": [task_id]}',
            content,
        )
        self.assertNotIn('"params": {"task_id": task_id}', content)

    def test_spawn_reviewer_tmux_uses_positional_params(self) -> None:
        content = Path("scripts/spawn_reviewer_tmux.sh").read_text(encoding="utf-8")
        self.assertIn(
            'payload = {"jsonrpc": "2.0", "method": "getTask", "id": 1, "params": [task_id]}',
            content,
        )
        self.assertNotIn('"params": {"task_id": task_id}', content)


if __name__ == "__main__":
    unittest.main()

