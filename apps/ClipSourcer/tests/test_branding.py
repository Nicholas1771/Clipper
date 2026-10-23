from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from clipsourcer.models import ClipCandidate
from clipsourcer.service import ClipSourcerService
from test_scoring import build_config
from common.creators import Creator


class BrandingAdmissionTests(unittest.TestCase):
    def candidate(self, name):
        return ClipCandidate.from_apify_item({
            "id": name, "text": "sample", "createTimeISO": datetime.now(timezone.utc).isoformat(),
            "playCount": 200000, "webVideoUrl": f"https://example.com/{name}",
            "authorMeta": {"name": "sample"},
        })

    def test_unbranded_sources_never_enter_cache(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp), require_source_kick_branding=True)
            service = ClipSourcerService(config, branding_check=lambda path, manifest: manifest["id"] == "good")
            attempted = set()

            def download(config, candidate):
                path = config.output_dir / f"{candidate.clip_id}.mp4"
                path.write_bytes(b"video")
                return path

            ranked = [(self.candidate("bad"), 2.0), (self.candidate("good"), 1.0)]
            with patch("clipsourcer.service.download_video_file", side_effect=download):
                accepted = service._screen_branding(ranked, attempted)
            service._save_candidate_cache([candidate for candidate, _ in accepted])
            cache = json.loads(config.candidate_cache_file.read_text())
            self.assertEqual([item["id"] for item in cache["items"]], ["good"])
            self.assertEqual(attempted, {"bad"})
            self.assertEqual(service._load_attempted_candidates(), {"bad"})

    def test_optional_branding_does_not_download_or_screen(self):
        with TemporaryDirectory() as tmp:
            service = ClipSourcerService(build_config(Path(tmp)))
            ranked = [(self.candidate("unbranded"), 1.0)]
            with patch("clipsourcer.service.download_video_file") as download:
                self.assertEqual(service._screen_branding(ranked, set()), ranked)
            download.assert_not_called()

    def test_detector_errors_do_not_admit_or_permanently_reject_candidates(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp), require_source_kick_branding=True)
            service = ClipSourcerService(config, branding_check=lambda *_: (_ for _ in ()).throw(RuntimeError("unavailable")))
            attempted = set()
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"video")
            with patch("clipsourcer.service.download_video_file", return_value=path):
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    service._screen_branding([(self.candidate("clip"), 1.0)], attempted)
            self.assertFalse(config.candidate_cache_file.exists())
            self.assertEqual(attempted, set())

    def test_branding_policy_invalidates_old_cache_scope(self):
        with TemporaryDirectory() as tmp:
            service = ClipSourcerService(build_config(Path(tmp)))
            before = service._candidate_cache_scope()
            service.config = replace(service.config, require_source_kick_branding=True)
            self.assertNotEqual(before, service._candidate_cache_scope())

    def test_scheduled_creator_is_screened_before_every_cache_write_and_snapshotted(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            creators = (
                Creator("n3on", 2, 1, {"STREAMER_NAME": "N3on", "SOURCE_HASHTAG": "n3on", "REQUIRE_SOURCE_KICK_BRANDING": "false"}),
                Creator("navaeh", 2, 1, {"STREAMER_NAME": "Nevaeh", "SOURCE_HASHTAG": "nevaehakira", "REQUIRE_SOURCE_KICK_BRANDING": "true"}),
            )
            config = build_config(root, creators=creators, download_video_files=True)
            config.output_dir.mkdir()
            (config.output_dir / "scheduler_state.json").write_text(json.dumps({
                "phase": "scheduled", "creator": "navaeh",
            }))
            raw = [self.candidate(name).raw_payload for name in ("bad", "good", "reserve")]
            fetched = []

            def fetch(effective):
                fetched.append(effective.source_hashtag)
                return raw

            def download(effective, candidate):
                effective.output_dir.mkdir(parents=True, exist_ok=True)
                path = effective.output_dir / f"{candidate.clip_id}.mp4"
                path.write_bytes(b"video")
                return path

            service = ClipSourcerService(
                config, fetch_items=fetch, branding_check=lambda _, manifest: manifest["id"] != "bad",
            )
            with patch("clipsourcer.service.download_video_file", side_effect=download), \
                 patch.object(service, "_save_candidate_cache", wraps=service._save_candidate_cache) as saved:
                outcome = service.run_once()
            self.assertEqual(outcome.status, "sourced")
            self.assertEqual(fetched, ["nevaehakira"])
            for call in saved.call_args_list:
                self.assertNotIn("bad", [candidate.clip_id for candidate in call.args[0]])
            snapshot = json.loads((outcome.result.manifest_path.parent.parent / "creator.json").read_text())
            self.assertEqual(snapshot["name"], "navaeh")
            self.assertEqual(snapshot["display_name"], "Nevaeh")
            self.assertIn("navaeh", service.config.candidate_cache_file.name)
            self.assertFalse(config.state_file.exists())
