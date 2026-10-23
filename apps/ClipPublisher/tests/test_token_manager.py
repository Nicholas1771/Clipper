import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from clippublisher import token_manager


class TokenReplacementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "token.json"
        self.path.write_text('{"access_token": "old"}', encoding="utf-8")
        self.original = self.path.read_bytes()
        self.config = SimpleNamespace(
            token_state_file=self.path, facebook_app_id="123",
            facebook_app_secret="secret", auto_refresh_token=False, refresh_buffer_days=7,
        )
        self.debug = {"is_valid": True, "app_id": "123", "expires_at": 4102444800}

    def test_replacement_overrides_old_state_and_auto_refresh_setting(self):
        with mock.patch.object(token_manager, "_debug_token", return_value=self.debug) as debug, \
             mock.patch.object(token_manager, "_exchange_for_long_lived", return_value=("long-lived", 3600)) as exchange:
            result = token_manager.replace_access_token(self.config, " new ")
        self.assertEqual(result, "long-lived")
        exchange.assert_called_once_with(self.config, "new")
        self.assertEqual(debug.call_args_list, [mock.call(self.config, "new"), mock.call(self.config, "long-lived")])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["access_token"], "long-lived")
        self.assertEqual(saved["source"], "replacement")
        self.assertEqual(saved["expires_at"], "2100-01-01T00:00:00+00:00")

    def test_invalid_or_wrong_app_token_preserves_state(self):
        for debug in ({"is_valid": False}, {"is_valid": True, "app_id": "other"}):
            with self.subTest(debug=debug), \
                 mock.patch.object(token_manager, "_debug_token", return_value=debug), \
                 mock.patch.object(token_manager, "_exchange_for_long_lived") as exchange:
                with self.assertRaises(ValueError):
                    token_manager.replace_access_token(self.config, "new")
                exchange.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.original)

    def test_failed_exchange_does_not_leak_credentials_or_change_state(self):
        with mock.patch.object(token_manager, "_debug_token", return_value=self.debug), \
             mock.patch.object(token_manager.requests, "get", side_effect=requests.ConnectionError("secret-token-in-url")):
            with self.assertRaises(RuntimeError) as caught:
                token_manager.replace_access_token(self.config, "new")
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_invalid_exchanged_token_preserves_state(self):
        with mock.patch.object(token_manager, "_debug_token", side_effect=[self.debug, {"is_valid": False}]), \
             mock.patch.object(token_manager, "_exchange_for_long_lived", return_value=("bad", 0)):
            with self.assertRaises(ValueError):
                token_manager.replace_access_token(self.config, "new")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_atomic_write_failure_preserves_state_and_cleans_temporary_file(self):
        with mock.patch.object(token_manager.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                token_manager._write_json(self.path, {"access_token": "new"})
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_empty_token_or_missing_app_credentials_preserves_state(self):
        with self.assertRaises(ValueError):
            token_manager.replace_access_token(self.config, " ")
        self.config.facebook_app_secret = ""
        with self.assertRaises(ValueError):
            token_manager.replace_access_token(self.config, "new")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_explicit_renewal_ignores_auto_refresh_switch_and_missing_expiry(self):
        with mock.patch.object(token_manager, "_debug_token", return_value={"is_valid": True}), \
             mock.patch.object(token_manager, "_exchange_for_long_lived", return_value=("renewed", 3600)) as exchange:
            result = token_manager.resolve_publish_access_token(self.config, force_refresh=True)
        self.assertEqual(result, "renewed")
        exchange.assert_called_once_with(self.config, "old")

    def test_explicit_renewal_missing_credentials_errors_instead_of_claiming_success(self):
        self.config.facebook_app_secret = ""
        with self.assertRaises(ValueError):
            token_manager.resolve_publish_access_token(self.config, force_refresh=True)
        self.assertEqual(self.path.read_bytes(), self.original)


class TokenCommandTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(self.cli, "available_accounts", return_value=["carootclips", "vrlclipmoments"])
        patcher.start()
        self.addCleanup(patcher.stop)

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "main.py"
        spec = importlib.util.spec_from_file_location("publisher_token_cli", path)
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    def test_profile_flags(self):
        for profile in ("carootclips", "vrlclipmoments"):
            with mock.patch("sys.argv", ["main.py", "--refresh-token", f"--{profile}", "--token=new"]):
                args = self.cli.parse_args()
            self.assertEqual(args.profile, profile)
            self.assertEqual(args.token, "new")

    def test_rejects_unsafe_argument_combinations(self):
        for flags in (["--token=new"], ["--token-prompt"], ["--refresh-token", "--once"],
                      ["--refresh-token", "--youtube-auth"], ["--refresh-token", "--token="],
                      ["--refresh-token", "--carootclips", "--vrlclipmoments"]):
            with self.subTest(flags=flags), mock.patch("sys.argv", ["main.py", *flags]), \
                 mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
                self.cli.parse_args()
            self.assertEqual(caught.exception.code, 2)

    def test_refresh_sets_profile_and_never_constructs_publisher(self):
        for profile, flag in (("carootclips", "--token=new"), ("vrlclipmoments", "--token-prompt")):
            with self.subTest(profile=profile), \
                 mock.patch("sys.argv", ["main.py", "--refresh-token", f"--{profile}", flag]), \
                 mock.patch.dict(os.environ, {"CLIPPER_PROFILE": "old"}), \
                 mock.patch.object(self.cli, "configure_logging"), \
                 mock.patch.object(self.cli.AppConfig, "from_repo_root") as config, \
                 mock.patch.object(self.cli, "QueuePublisherRunner") as runner, \
                 mock.patch.object(self.cli.getpass, "getpass", return_value="new"), \
                 mock.patch.object(token_manager, "replace_access_token") as replace:
                self.assertEqual(self.cli.main(), 0)
                self.assertEqual(os.environ["CLIPPER_PROFILE"], profile)
                config.assert_called_once_with(self.cli.APP_ROOT, resolve_media_url=False)
                replace.assert_called_once_with(config.return_value, "new")
                runner.assert_not_called()

    def test_token_stdin_reads_one_line_and_rejects_empty(self):
        with mock.patch("sys.argv", ["main.py", "--refresh-token", "--carootclips", "--token-stdin"]), \
             mock.patch.dict(os.environ), \
             mock.patch("sys.stdin", io.StringIO(" piped \n")), \
             mock.patch.object(self.cli, "configure_logging"), \
             mock.patch.object(self.cli.AppConfig, "from_repo_root") as config, \
             mock.patch.object(token_manager, "replace_access_token") as replace:
            self.assertEqual(self.cli.main(), 0)
            replace.assert_called_once_with(config.return_value, "piped")
        with mock.patch("sys.argv", ["main.py", "--refresh-token", "--carootclips", "--token-stdin"]), \
             mock.patch.dict(os.environ), \
             mock.patch("sys.stdin", io.StringIO("")), \
             mock.patch.object(self.cli, "configure_logging"), \
             mock.patch.object(self.cli.AppConfig, "from_repo_root"), \
             mock.patch.object(self.cli, "send_failure_alert"), \
             mock.patch.object(self.cli.logger, "exception"), \
             mock.patch.object(token_manager, "replace_access_token") as replace:
            self.assertEqual(self.cli.main(), 1)
            replace.assert_not_called()

    def test_legacy_refresh_still_supported(self):
        with mock.patch("sys.argv", ["main.py", "--refresh-token", "--carootclips"]), \
             mock.patch.dict(os.environ), \
             mock.patch.object(self.cli, "configure_logging"), \
             mock.patch.object(self.cli.AppConfig, "from_repo_root") as config, \
             mock.patch.object(self.cli, "resolve_publish_access_token") as refresh, \
             mock.patch.object(self.cli, "QueuePublisherRunner") as runner:
            self.assertEqual(self.cli.main(), 0)
            refresh.assert_called_once_with(config.return_value, force_refresh=True)
            runner.assert_not_called()
