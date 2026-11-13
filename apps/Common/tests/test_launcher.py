import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("account_launcher", REPO_ROOT / "run_all_apps.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class AccountEnabledTests(unittest.TestCase):
    def test_account_switch_defaults_and_boolean_values(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            (root / "accounts").mkdir()
            config = root / "accounts" / "example.env"
            for value, expected in (("", True), ("true", True), ("false", False),
                                    ("1", True), ("0", False), ("off", False)):
                with self.subTest(value=value):
                    config.write_text(f"ACCOUNT_ENABLED={value}\n", encoding="utf-8")
                    self.assertEqual(launcher.is_account_enabled(root, "example"), expected)

    def test_process_override_and_account_isolation(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            (root / "accounts").mkdir()
            disabled = root / "accounts" / "disabled.env"
            disabled.write_text("ACCOUNT_ENABLED=false\n", encoding="utf-8")
            (root / "accounts" / "enabled.env").write_text("CREATORS=one:8:1\n", encoding="utf-8")
            self.assertFalse(launcher.is_account_enabled(root, "disabled"))
            self.assertTrue(launcher.is_account_enabled(root, "enabled"))
            self.assertNotIn("ACCOUNT_ENABLED", os.environ)
            with patch.dict(os.environ, {"ACCOUNT_ENABLED": "true"}):
                self.assertTrue(launcher.is_account_enabled(root, "disabled"))
            with patch.dict(os.environ, {"ACCOUNT_ENABLED": "false"}):
                self.assertFalse(launcher.is_account_enabled(root, "enabled"))
            self.assertEqual(disabled.read_text(encoding="utf-8"), "ACCOUNT_ENABLED=false\n")

    def test_invalid_boolean_and_missing_account_fail(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            (root / "accounts").mkdir()
            (root / "accounts" / "example.env").write_text("ACCOUNT_ENABLED=typo\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid boolean"):
                launcher.is_account_enabled(root, "example")
            with self.assertRaises(FileNotFoundError):
                launcher.is_account_enabled(root, "missing")

    def test_disabled_launcher_starts_no_apps_and_idles(self):
        with patch("sys.argv", ["run_all_apps.py", "--kickzsclipz"]), \
             patch.object(launcher, "is_account_enabled", return_value=False), \
             patch.object(launcher, "build_launch_plan") as plan, \
             patch.object(launcher, "launch_processes") as launch, \
             patch.object(launcher, "wait_while_disabled", return_value=0) as wait, \
             patch("builtins.print"):
            self.assertEqual(launcher.main(), 0)
            wait.assert_called_once_with()
            plan.assert_not_called()
            launch.assert_not_called()

    def test_disabled_one_off_commands_exit_without_work(self):
        for flag in ("--dry-run", "--schedule", "--now", "--stop-after=editor", "--test"):
            with self.subTest(flag=flag), \
                 patch("sys.argv", ["run_all_apps.py", "--kickzsclipz", flag]), \
                 patch.object(launcher, "is_account_enabled", return_value=False), \
                 patch.object(launcher, "build_launch_plan") as plan, \
                 patch.object(launcher, "launch_processes") as launch, \
                 patch.object(launcher, "wait_while_disabled") as wait, \
                 patch("builtins.print"):
                self.assertEqual(launcher.main(), 0)
                plan.assert_not_called()
                launch.assert_not_called()
                wait.assert_not_called()

    def test_enabled_launcher_preserves_existing_arguments(self):
        with patch("sys.argv", ["run_all_apps.py", "--carootclips", "--now"]), \
             patch.object(launcher, "is_account_enabled", return_value=True), \
             patch.object(launcher, "launch_processes", return_value=0) as launch:
            self.assertEqual(launcher.main(), 0)
            self.assertEqual(launch.call_args.args[0][0],
                             ("ClipScheduler", REPO_ROOT / "apps" / "ClipScheduler", ["--now"]))
            self.assertEqual(len(launch.call_args.args[0]), 5)
            self.assertEqual(launch.call_args.kwargs,
                             {"stop_mode": False, "test_mode": False, "profile": "carootclips"})

    def test_disabled_wait_handles_interrupt(self):
        with patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(launcher.wait_while_disabled(), 0)
