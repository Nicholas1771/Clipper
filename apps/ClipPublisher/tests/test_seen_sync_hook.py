import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clippublisher import runner as runner_module

from test_youtube import make_runner


class SeenSyncHookTests(unittest.TestCase):
    def record(self, env: dict[str, str], run: mock.Mock) -> Path:
        with tempfile.TemporaryDirectory() as tmp:
            _, runner, seen = make_runner(tmp, ["instagram"])
            with mock.patch.dict("os.environ", env, clear=False), \
                 mock.patch.object(runner, "_sourcer_seen_ids_path", lambda: seen), \
                 mock.patch.object(runner_module.subprocess, "run", run):
                runner._record_seen_id("7690000000000000001")
                self.assertIn("7690000000000000001", seen.read_text())
        return seen

    def test_disabled_by_default(self):
        run = mock.Mock()
        self.record({"SYNC_SEEN_TO_PRODUCTION": "", "CLIPPER_PROFILE": "n3on"}, run)
        run.assert_not_called()

    def test_enabled_runs_sync_for_active_profile(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="[n3on] ok", stderr=""))
        self.record({"SYNC_SEEN_TO_PRODUCTION": "true", "CLIPPER_PROFILE": "n3on"}, run)
        command = run.call_args.args[0]
        self.assertTrue(command[1].endswith("sync_seen_ids.py"))
        self.assertEqual("--n3on", command[2])

    def test_sync_failure_never_raises(self):
        run = mock.Mock(side_effect=OSError("no ssh"))
        with self.assertLogs(runner_module.logger, "WARNING") as logs:
            self.record({"SYNC_SEEN_TO_PRODUCTION": "true", "CLIPPER_PROFILE": "n3on"}, run)
        self.assertIn("sync_seen_ids.py --n3on", logs.output[0])


if __name__ == "__main__":
    unittest.main()
