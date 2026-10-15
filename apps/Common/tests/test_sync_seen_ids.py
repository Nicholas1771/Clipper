from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import sync_seen_ids  # noqa: E402


class MergeSeenIdsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, ids: list[str], **extra) -> None:
        self.state.write_text(json.dumps({"seen_ids": ids, **extra}), encoding="utf-8")

    def test_merge_only_appends_and_preserves_other_keys(self) -> None:
        self.write(["111111", "222222"], updated_at="old", note="keep")
        result = sync_seen_ids.merge_ids(self.state, {"222222", "333333"})
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(["111111", "222222", "333333"], payload["seen_ids"])
        self.assertEqual("keep", payload["note"])
        self.assertEqual({"before": 2, "added": 1, "after": 3}, result)

    def test_dry_run_and_no_new_ids_do_not_write(self) -> None:
        self.write(["111111"], updated_at="old")
        original = self.state.read_text(encoding="utf-8")
        self.assertEqual(1, sync_seen_ids.merge_ids(self.state, {"222222"}, dry_run=True)["added"])
        self.assertEqual(0, sync_seen_ids.merge_ids(self.state, {"111111"})["added"])
        self.assertEqual(original, self.state.read_text(encoding="utf-8"))

    def test_missing_file_is_created(self) -> None:
        sync_seen_ids.merge_ids(self.state, {"444444"})
        self.assertEqual(["444444"], json.loads(self.state.read_text(encoding="utf-8"))["seen_ids"])

    def test_refuses_non_object_history(self) -> None:
        self.state.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ValueError):
            sync_seen_ids.merge_ids(self.state, {"444444"})
        self.assertEqual("[1, 2]", self.state.read_text(encoding="utf-8"))

    def test_rejects_invalid_ids(self) -> None:
        with self.assertRaises(ValueError):
            sync_seen_ids.valid_ids(["123456", "abc; rm -rf"])
        with self.assertRaises(ValueError):
            sync_seen_ids.valid_ids({"123456": 1})

    def test_merge_stdin_cli(self) -> None:
        self.write(["111111"])
        out = io.StringIO()
        profile = sync_seen_ids.discover_profiles()[0]
        with mock.patch.object(sync_seen_ids, "resolve_state_file", return_value=self.state), \
             mock.patch.object(sys, "stdin", io.StringIO(json.dumps(["111111", "555555"]))), \
             mock.patch.object(sys, "stdout", out):
            self.assertEqual(0, sync_seen_ids.main([f"--{profile}", "--merge-stdin"]))
        self.assertEqual({"before": 1, "added": 1, "after": 2}, json.loads(out.getvalue()))
        self.assertIn("555555", self.state.read_text(encoding="utf-8"))

    def test_push_sends_ids_on_stdin_not_arguments(self) -> None:
        self.write(["111111", "222222"])
        completed = mock.Mock(returncode=0, stdout='{"before": 5, "added": 1, "after": 6}\n', stderr="")
        with mock.patch.object(sync_seen_ids, "resolve_state_file", return_value=self.state), \
             mock.patch.dict("os.environ", {"PRODUCTION_HOST": "example.invalid"}), \
             mock.patch.object(sync_seen_ids.subprocess, "run", return_value=completed) as run:
            result = sync_seen_ids.push("n3on")
        command = run.call_args.args[0]
        self.assertNotIn("111111", " ".join(command))
        self.assertIn("--merge-stdin", command[-1])
        self.assertEqual(["111111", "222222"], json.loads(run.call_args.kwargs["input"]))
        self.assertEqual(2, result["local"])
        self.assertEqual(1, result["added"])


if __name__ == "__main__":
    unittest.main()
