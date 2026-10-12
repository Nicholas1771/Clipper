import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clippublisher import runner as runner_module
from clippublisher.config import AppConfig


def make_item(queue: Path, name: str) -> Path:
    run = queue / name
    (run / "source").mkdir(parents=True)
    (run / "editor").mkdir()
    (run / "source" / "c.json").write_text(json.dumps({"clip_id": "123", "author_handle": "a"}), encoding="utf-8")
    (run / "editor" / "c_edited.mp4").write_bytes(b"x")
    (run / "queue_state.json").write_text(json.dumps({"phase": "edited"}), encoding="utf-8")
    return run


class SingleAttemptTests(unittest.TestCase):
    def test_failed_publish_is_attempted_once_and_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "Queue"
            make_item(queue, "0001_x")
            config = mock.MagicMock(spec=AppConfig)
            config.queue_dir = queue
            config.timezone_name = "UTC"
            config.ready_stability_seconds = 0
            config.poll_interval_seconds = 0
            runner = runner_module.QueuePublisherRunner(config)
            calls = []

            def boom(*a, **k):
                calls.append(1)
                raise RuntimeError("status_code=ERROR")

            seen = Path(tmp) / "state.json"
            with mock.patch.object(runner_module, "publish_to_instagram", boom), \
                 mock.patch.object(runner_module, "resolve_publish_access_token", lambda c: "t"), \
                 mock.patch.object(runner_module, "send_failure_alert"), \
                 mock.patch.object(runner, "_sourcer_seen_ids_path", lambda: seen):
                with self.assertRaises(RuntimeError):
                    runner.process_one_ready_item()
                self.assertFalse(runner.process_one_ready_item())
            self.assertEqual(len(calls), 1)
            self.assertEqual([p.name for p in queue.iterdir() if p.name not in {"processing", "Archive", "scheduler_state.json"}], [])
            self.assertTrue((queue / "Archive" / "0001_x_failed").exists())
            self.assertIn("123", json.loads(seen.read_text())["seen_ids"])


if __name__ == "__main__":
    unittest.main()

