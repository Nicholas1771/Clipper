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
    def test_scheduler_waits_until_publish_and_archive_complete(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ClipScheduler" / "src"))
        from clipscheduler.runner import ClipSchedulerRunner

        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "Queue"
            make_item(queue, "0001_x")
            (queue / "scheduler_state.json").write_text(json.dumps({
                "phase": "edited", "platforms": ["instagram"], "slot_index": 0, "completed_slots": 0,
            }))
            config = mock.MagicMock(spec=AppConfig)
            config.queue_dir = queue
            config.timezone_name = "UTC"
            config.ready_stability_seconds = 0
            runner = runner_module.QueuePublisherRunner(config)
            scheduler = ClipSchedulerRunner.__new__(ClipSchedulerRunner)
            scheduler.config = config

            def publish(*args, **kwargs):
                self.assertEqual("pending", scheduler.stage_status("publisher"))
                state = json.loads((queue / "scheduler_state.json").read_text())
                self.assertEqual("publishing", state["phase"])
                self.assertEqual(0, state["completed_slots"])
                return {"id": "instagram-post"}

            original_archive = runner._archive_completed_item

            def archive(path):
                self.assertEqual("pending", scheduler.stage_status("publisher"))
                self.assertEqual("success", json.loads((path / "publisher" / "publish_state.json").read_text())["status"])
                return original_archive(path)

            with mock.patch.object(runner_module, "publish_to_instagram", side_effect=publish), \
                 mock.patch.object(runner_module, "resolve_publish_access_token", return_value="token"), \
                 mock.patch.object(runner_module, "send_posted_alert"), \
                 mock.patch.object(runner, "_record_seen_id"), \
                 mock.patch.object(runner, "_archive_completed_item", side_effect=archive):
                self.assertTrue(runner.process_one_ready_item())
            self.assertEqual("reached", scheduler.stage_status("publisher"))
            self.assertEqual(1, json.loads((queue / "scheduler_state.json").read_text())["completed_slots"])
            self.assertTrue((queue / "Archive" / "0001_x").exists())

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
            self.assertFalse(seen.exists())
            self.assertEqual("publish_failed", json.loads((queue / "scheduler_state.json").read_text())["phase"])


if __name__ == "__main__":
    unittest.main()
