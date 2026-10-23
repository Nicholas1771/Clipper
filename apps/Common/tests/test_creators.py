from dataclasses import dataclass
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from common.creators import config_for_run, configured_creators, creator_payload
from common.env import load_app_env


class CreatorConfigTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        (root / "apps" / "Common").mkdir(parents=True)
        (root / "accounts").mkdir()
        (root / "creators").mkdir()
        (root / "README.md").touch()
        (root / "accounts" / "account.env").write_text(
            "ACCOUNT_NAME=Example\nCREATORS=one:3:1,two:2:0\nINSTAGRAM_USER_ID=account-id\n"
        )
        for name in ("one", "two"):
            (root / "creators" / f"{name}.env").write_text(
                f"STREAMER_NAME={name.title()}\nSTREAMER_HASHTAG={name}\n"
                f"OVERLAY_IMAGE_PATH={name}.png\nSOURCE_HASHTAG={name}\n"
                f"REQUIRE_SOURCE_KICK_BRANDING={'true' if name == 'two' else 'false'}\n"
            )

    def test_account_totals_paths_and_no_local_env(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            self.make_repo(root)
            (root / "accounts" / "account.local.env").write_text("INSTAGRAM_USER_ID=wrong\n")
            os.environ["CLIPPER_PROFILE"] = "account"
            load_app_env(root / "apps" / "Common")
            self.assertEqual(os.environ["INSTAGRAM_USER_ID"], "account-id")
            self.assertEqual(os.environ["INSTAGRAM_POSTS_PER_DAY"], "5")
            self.assertEqual(os.environ["YOUTUBE_POSTS_PER_DAY"], "1")
            self.assertEqual(os.environ["STATE_FILE"], "data/state/Example/state.json")
            self.assertEqual(os.environ["QUEUE_DIR"], "data/state/Example/queue")
            creators = configured_creators(root)
            self.assertEqual([c.name for c in creators], ["one", "two"])
            self.assertEqual(creators[1].settings["REQUIRE_SOURCE_KICK_BRANDING"], "true")

    def test_invalid_mapping_and_missing_creator_fail(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            for mapping in ("one:1", "one:-1:0", "../one:1:0", "one:1:0,one:2:0"):
                with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                    configured_creators(root, {"CREATORS": mapping})
            with self.assertRaises(FileNotFoundError):
                configured_creators(root, {"CREATORS": "missing:1:0"})

    def test_creator_cannot_override_account_credentials(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            with (root / "creators" / "one.env").open("a") as handle:
                handle.write("INSTAGRAM_USER_ID=wrong\n")
            with self.assertRaisesRegex(ValueError, "Unsupported settings"):
                configured_creators(root, {"CREATORS": "one:1:0"})

    def test_run_snapshot_selects_creator_without_mutating_environment(self):
        @dataclass(frozen=True)
        class Config:
            streamer_name: str = "One"
            streamer_hashtag: str = "one"

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            creator = configured_creators(root, {"CREATORS": "two:2:1"})[0]
            run = root / "run"
            run.mkdir()
            (run / "creator.json").write_text(json.dumps(creator_payload(creator)))
            original = Config()
            actual = config_for_run(original, run)
            self.assertEqual(actual.streamer_name, "Two")
            self.assertEqual(actual.streamer_hashtag, "two")
            self.assertEqual(original.streamer_name, "One")
