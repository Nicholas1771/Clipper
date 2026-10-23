from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

APP_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = APP_ROOT / "src"
COMMON_SRC_ROOT = APP_ROOT.parent / "Common" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(COMMON_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_SRC_ROOT))

from clipsourcer.config import AppConfig
from clipsourcer.downloads import _download_all_video_files_for_test, download_video_file
from clipsourcer.models import ClipCandidate
from clipsourcer.scoring import has_minimum_views, matches_keyword, rank_candidates, viral_score
from clipsourcer.service import (
    ClipSourcerService,
    _log_candidate_diagnostics,
    _log_play_count_distribution,
)
from clipsourcer.storage import build_clip_base_path, save_seen_ids


def build_candidate(
    clip_id: str,
    *,
    hours_ago: float,
    text: str,
    hashtags: list[str] | None = None,
    plays: int = 0,
    likes: int = 0,
    shares: int = 0,
    comments: int = 0,
    saves: int = 0,
) -> ClipCandidate:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ClipCandidate(
        clip_id=clip_id,
        url=f"https://www.tiktok.com/@example/video/{clip_id}",
        text=text,
        created_at=now - timedelta(hours=hours_ago),
        author_handle="example",
        hashtags=hashtags or [],
        plays=plays,
        likes=likes,
        shares=shares,
        comments=comments,
        saves=saves,
        media_urls=[],
        raw_payload={"id": clip_id},
    )


def build_config(root: Path, **overrides: object) -> AppConfig:
    defaults: dict[str, object] = {
        "apify_token": "token",
        "apify_actor_id": "clockworks~tiktok-scraper",
        "source_mode": "hashtag",
        "source_hashtag": "sample",
        "source_profiles": [],
        "keyword_filter": "sample",
        "candidate_cache_file": root / "data" / "candidate_cache.json",
        "poll_interval_minutes": 15,
        "lookback_hours": 4,
        "results_per_page": 25,
        "minimum_view_count": 0,
        "output_dir": root / "queue",
        "state_file": root / "data" / "state.json",
        "download_video_files": False,
        "authorized_download_profiles": [],
        "source_attempts_per_slot": 3,
        "source_timezone_name": "America/New_York",
        "allow_download_bypass": False,
    }
    defaults.update(overrides)
    return AppConfig(**defaults)


class ScoringTests(unittest.TestCase):
    def test_keyword_match_works_for_caption_and_hashtag(self) -> None:
        caption_match = build_candidate("1", hours_ago=1, text="Best #sample moment", hashtags=[])
        hashtag_match = build_candidate("2", hours_ago=1, text="Best moment", hashtags=["sample"])
        miss = build_candidate("3", hours_ago=1, text="Best moment", hashtags=["stream"])

        self.assertTrue(matches_keyword(caption_match, "sample"))
        self.assertTrue(matches_keyword(hashtag_match, "#sample"))
        self.assertFalse(matches_keyword(miss, "sample"))

    def test_viral_score_rewards_recency(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = build_candidate("1", hours_ago=1, text="sample", plays=1000, likes=80, shares=10, comments=5, saves=2)
        older = build_candidate("2", hours_ago=3, text="sample", plays=1000, likes=80, shares=10, comments=5, saves=2)

        self.assertGreater(viral_score(newer, now), viral_score(older, now))

    def test_minimum_views_filter_works(self) -> None:
        candidate = build_candidate("1", hours_ago=1, text="sample", plays=1500)

        self.assertTrue(has_minimum_views(candidate, 1000))
        self.assertFalse(has_minimum_views(candidate, 2000))

    def test_rank_candidates_keeps_seen_ids_for_final_selection_only(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candidates = [
            build_candidate("seen", hours_ago=1, text="sample", plays=5000, likes=500),
            build_candidate("fresh", hours_ago=1, text="sample", plays=2000, likes=150),
            build_candidate("stale", hours_ago=6, text="sample", plays=9000, likes=900),
        ]

        ranked = rank_candidates(
            candidates,
            now=now,
            lookback_hours=4,
            keyword="sample",
            minimum_view_count=1000,
            seen_ids={"seen"},
        )

        self.assertEqual(["seen", "fresh"], [candidate.clip_id for candidate, _score in ranked])


class ServiceTests(unittest.TestCase):
    def test_failed_cached_download_is_removed_without_marking_seen(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            item = {
                "id": "failed",
                "createTimeISO": datetime.now(timezone.utc).isoformat(),
                "text": "sample",
                "playCount": 150_000,
            }
            service = ClipSourcerService(config)
            service._save_candidate_cache([ClipCandidate.from_apify_item(item)])
            with patch("clipsourcer.service.download_video_file", side_effect=RuntimeError("download failed")):
                outcome = service.run_once(bypass_schedule=True)
            self.assertEqual("no_match", outcome.status)
            self.assertFalse(config.state_file.exists())
            self.assertEqual([], json.loads(config.candidate_cache_file.read_text())["items"])
            self.assertEqual({"failed"}, service._load_attempted_candidates())

            restarted = ClipSourcerService(config, fetch_items=lambda _config: [item])
            with patch("clipsourcer.service.download_video_file") as download:
                outcome = restarted.run_once(bypass_schedule=True)
            self.assertEqual("no_match", outcome.status)
            download.assert_not_called()

    def test_queue_write_failure_does_not_leave_candidate_cached_or_seen(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            item = {
                "id": "queue-failed",
                "createTimeISO": datetime.now(timezone.utc).isoformat(),
                "text": "sample",
            }
            service = ClipSourcerService(config, fetch_items=lambda _config: [item])
            with patch("clipsourcer.service.write_queue_state", side_effect=OSError("disk failure")):
                outcome = service.run_once(bypass_schedule=True)
            self.assertEqual("error", outcome.status)
            self.assertFalse(config.state_file.exists())
            self.assertEqual([], json.loads(config.candidate_cache_file.read_text())["items"])
            self.assertEqual({"queue-failed"}, service._load_attempted_candidates())

    def test_cache_logs_each_candidate_only_after_successful_save(self) -> None:
        with TemporaryDirectory() as temp_dir:
            service = ClipSourcerService(build_config(Path(temp_dir)))
            candidates = [
                build_candidate("first", hours_ago=1, text="sample", plays=150_000),
                build_candidate("second", hours_ago=1, text="sample", plays=200_000),
            ]

            with self.assertLogs("clipsourcer.service", level="INFO") as captured:
                service._save_candidate_cache(candidates)

            self.assertIn("Candidate cache saved: size=2 clip(s).", captured.output[0])
            self.assertEqual(3, len(captured.output))
            for candidate, message in zip(candidates, captured.output[1:]):
                self.assertIn(
                    f"Saved candidate to cache: id={candidate.clip_id} author=example views={candidate.plays}",
                    message,
                )
                self.assertIn("viral_score=", message)
                self.assertIn(f"url={candidate.url}", message)

            with patch.object(Path, "replace", side_effect=OSError("cache write failed")):
                with self.assertLogs("clipsourcer.service", level="INFO") as failed:
                    service._save_candidate_cache(candidates)

            self.assertTrue(any("Could not persist candidate cache" in message for message in failed.output))
            self.assertFalse(any("Saved candidate to cache:" in message for message in failed.output))

    def test_candidate_diagnostics_log_selection_and_rejection_details(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        eligible = build_candidate(
            "eligible",
            hours_ago=1,
            text="sample clip",
            plays=150_000,
            likes=1200,
            comments=80,
            shares=40,
            saves=20,
        )
        seen = build_candidate(
            "seen",
            hours_ago=1,
            text="sample clip",
            plays=200_000,
        )
        below_threshold = build_candidate(
            "below-threshold",
            hours_ago=1,
            text="sample clip",
            plays=99_999,
        )

        with self.assertLogs("clipsourcer.service", level="DEBUG") as captured:
            _log_candidate_diagnostics(
                [eligible, seen, below_threshold],
                origin="Apify",
                now=now,
                lookback_hours=4,
                keyword="sample",
                minimum_view_count=100_000,
                seen_ids={"seen"},
                ranked=[(eligible, 20.0), (seen, 10.0)],
                unseen_ranked=[(eligible, 20.0)],
            )

        self.assertEqual(3, len(captured.output))
        self.assertIn(
            "Apify candidate: id=eligible author=example views=150000 likes=1200 comments=80 "
            "shares=40 saves=20",
            captured.output[0],
        )
        self.assertIn("eligible_rank=1 selection_rank=1", captured.output[0])
        self.assertIn("meets_min_views=True (minimum=100000) seen=False rejected_by=none", captured.output[0])
        self.assertIn("url=https://www.tiktok.com/@example/video/eligible", captured.output[0])
        self.assertIn("eligible_rank=2 selection_rank=-", captured.output[1])
        self.assertIn("meets_min_views=True (minimum=100000) seen=True rejected_by=already_seen", captured.output[1])
        self.assertIn("eligible_rank=- selection_rank=-", captured.output[2])
        self.assertIn(
            "meets_min_views=False (minimum=100000) seen=False rejected_by=below_min_views",
            captured.output[2],
        )

    def test_play_count_distribution_is_logged_in_ranges(self) -> None:
        candidates = [
            build_candidate("1", hours_ago=1, text="sample", plays=0),
            build_candidate("2", hours_ago=1, text="sample", plays=999),
            build_candidate("3", hours_ago=1, text="sample", plays=1_000),
            build_candidate("4", hours_ago=1, text="sample", plays=10_000),
            build_candidate("5", hours_ago=1, text="sample", plays=100_000),
            build_candidate("6", hours_ago=1, text="sample", plays=1_000_000),
            build_candidate("7", hours_ago=1, text="sample", plays=10_000_000),
        ]

        with self.assertLogs("clipsourcer.service", level="INFO") as captured:
            _log_play_count_distribution(candidates)

        self.assertIn(
            "Apify result play-count distribution (total=7): "
            "<1,000: 2, 1,000-9,999: 1, 10,000-99,999: 1, "
            "100,000-999,999: 1, 1,000,000-9,999,999: 1, >=10,000,000: 1",
            captured.output[0],
        )

    def test_without_scheduler_marker_sourcing_waits(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ClipSourcerService(build_config(root))
            with patch.object(service, "fetch_items") as fetch:
                outcome = service.run_once()
            self.assertEqual(outcome.status, "skipped")
            fetch.assert_not_called()

    def test_run_once_bypasses_schedule_when_forced(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)

            recent = datetime.now(timezone.utc) - timedelta(minutes=45)
            items = [
                {
                    "id": "winner",
                    "webVideoUrl": "https://www.tiktok.com/@winner/video/1",
                    "text": "sample clip",
                    "createTimeISO": recent.isoformat(),
                    "authorMeta": {"name": "winner"},
                    "hashtags": [{"name": "sample"}],
                    "playCount": 50000,
                    "diggCount": 4000,
                    "shareCount": 300,
                    "commentCount": 120,
                    "collectCount": 90,
                }
            ]

            service = ClipSourcerService(config, fetch_items=lambda _config: items)
            outcome = service.run_once(bypass_schedule=True)

            self.assertEqual("sourced", outcome.status)

    def test_run_once_marks_scheduler_slot_failed_after_three_attempts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)
            scheduler_state_path = config.output_dir / "scheduler_state.json"
            scheduler_state_path.parent.mkdir(parents=True, exist_ok=True)
            scheduler_state_path.write_text(
                json.dumps(
                    {
                        "date": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
                        "phase": "scheduled",
                        "slot_index": 2,
                        "completed_slots": 2,
                    }
                ),
                encoding="utf-8",
            )

            service = ClipSourcerService(config, fetch_items=lambda _config: [])
            outcome = service.run_once()

            self.assertEqual("source_failed", outcome.status)
            payload = json.loads(scheduler_state_path.read_text(encoding="utf-8"))
            self.assertEqual("source_failed", payload["phase"])
            self.assertEqual(3, payload["source_attempts"])

    def test_clear_seen_ids_writes_empty_set(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_file = root / "state.json"
            save_seen_ids(state_file, {"a", "b"})
            save_seen_ids(state_file, set())
            payload = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual([], payload["seen_ids"])

    def test_run_once_writes_manifest_for_best_new_candidate(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)

            recent = datetime.now(timezone.utc) - timedelta(minutes=45)
            less_recent = datetime.now(timezone.utc) - timedelta(hours=2)
            items = [
                {
                    "id": "winner",
                    "webVideoUrl": "https://www.tiktok.com/@winner/video/1",
                    "text": "sample clip",
                    "createTimeISO": recent.isoformat(),
                    "authorMeta": {"name": "winner"},
                    "hashtags": [{"name": "sample"}],
                    "playCount": 50000,
                    "diggCount": 4000,
                    "shareCount": 300,
                    "commentCount": 120,
                    "collectCount": 90,
                },
                {
                    "id": "runner-up",
                    "webVideoUrl": "https://www.tiktok.com/@runner/video/1",
                    "text": "sample clip",
                    "createTimeISO": less_recent.isoformat(),
                    "authorMeta": {"name": "runner"},
                    "hashtags": [{"name": "sample"}],
                    "playCount": 30000,
                    "diggCount": 2500,
                    "shareCount": 100,
                    "commentCount": 60,
                    "collectCount": 40,
                },
            ]

            fetch_count = 0

            def fetch_items(_config: AppConfig) -> list[dict]:
                nonlocal fetch_count
                fetch_count += 1
                return items

            service = ClipSourcerService(config, fetch_items=fetch_items)
            with self.assertLogs("clipsourcer.service", level="INFO") as fresh_logs:
                outcome = service.run_once(bypass_schedule=True)

            self.assertTrue(any("Candidate cache size at sourcing start: 0" in line for line in fresh_logs.output))
            self.assertTrue(any("Candidate source: Apify;" in line for line in fresh_logs.output))
            self.assertTrue(any("Candidate cache saved: size=1 clip(s)." in line for line in fresh_logs.output))

            self.assertEqual("sourced", outcome.status)
            assert outcome.result is not None
            self.assertTrue(outcome.result.manifest_path.exists())
            self.assertIn("winner", outcome.result.manifest_path.read_text(encoding="utf-8"))
            self.assertIsNone(outcome.result.downloaded_video_path)
            self.assertFalse(config.state_file.exists())
            self.assertEqual({"winner"}, service._load_attempted_candidates())

            restarted_service = ClipSourcerService(config, fetch_items=fetch_items)
            with self.assertLogs("clipsourcer.service", level="INFO") as cache_logs:
                second = restarted_service.run_once(bypass_schedule=True)
            self.assertTrue(any("Candidate cache size at sourcing start: 1" in line for line in cache_logs.output))
            self.assertTrue(any("from the persistent cache; skipping Apify fetch." in line for line in cache_logs.output))
            self.assertTrue(any("Candidate cache saved: size=0 clip(s)." in line for line in cache_logs.output))
            self.assertTrue(any(
                "Using candidate from cache: id=runner-up author=runner views=30000 viral_score=" in line
                for line in cache_logs.output
            ))
            self.assertFalse(any("Saved candidate to cache:" in line for line in cache_logs.output))
            self.assertFalse(any("Candidate cache saved: size=1 clip(s)." in line for line in cache_logs.output))
            self.assertEqual("sourced", second.status)
            assert second.result is not None
            self.assertIn("runner-up", second.result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(1, fetch_count)

    def test_run_once_downloads_authorized_video_when_media_url_is_present(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(
                root,
                source_mode="profiles",
                source_profiles=["winner"],
                download_video_files=True,
                authorized_download_profiles=["winner"],
            )

            items = [
                {
                    "id": "winner",
                    "webVideoUrl": "https://www.tiktok.com/@winner/video/1",
                    "text": "sample clip",
                    "createTimeISO": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
                    "authorMeta": {"name": "winner"},
                    "hashtags": [{"name": "sample"}],
                    "playCount": 50000,
                    "diggCount": 4000,
                    "shareCount": 300,
                    "commentCount": 120,
                    "collectCount": 90,
                    "mediaUrls": ["https://cdn.example.com/winner.mp4"],
                }
            ]

            class FakeResponse:
                headers = {"Content-Type": "video/mp4"}

                def raise_for_status(self) -> None:
                    return None

                def iter_content(self, chunk_size: int):
                    del chunk_size
                    yield b"video-bytes"

            with patch("clipsourcer.downloads.download_with_ytdlp") as mock_ytdlp, patch(
                "clipsourcer.downloads.download_with_tikwm"
            ) as mock_tikwm, patch("clipsourcer.downloads.download_with_apify") as mock_apify:
                def write_mock_video(*_args) -> Path:
                    path = root / "queue" / "20260101T000000Z_winner_winner.mp4"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"video-bytes")
                    return path

                mock_ytdlp.side_effect = write_mock_video
                mock_tikwm.side_effect = AssertionError("TikWM fallback should not run")
                mock_apify.side_effect = AssertionError("Apify fallback should not run")
                service = ClipSourcerService(config, fetch_items=lambda _config: items)
                outcome = service.run_once(bypass_schedule=True)

            self.assertEqual("sourced", outcome.status)
            assert outcome.result is not None
            self.assertTrue(outcome.result.manifest_path.exists())
            self.assertIsNotNone(outcome.result.downloaded_video_path)
            assert outcome.result.downloaded_video_path is not None
            self.assertTrue(outcome.result.downloaded_video_path.exists())
            self.assertEqual(outcome.result.manifest_path.parent, outcome.result.downloaded_video_path.parent)
            self.assertEqual(b"video-bytes", outcome.result.downloaded_video_path.read_bytes())
            mock_ytdlp.assert_called_once()


class TestModeTests(unittest.TestCase):
    def _service(self, root: Path) -> tuple[ClipSourcerService, Path]:
        config = build_config(root)
        fetch_calls: list[object] = []
        service = ClipSourcerService(config, fetch_items=lambda cfg: fetch_calls.append(cfg) or [], test_mode=True)
        service.fetch_calls = fetch_calls
        return service, config.output_dir

    def test_test_mode_copies_test_source_without_fetching(self) -> None:
        with TemporaryDirectory() as tmp:
            service, queue = self._service(Path(tmp))
            test_source = queue / "source"
            test_source.mkdir(parents=True)
            (test_source / "clip_1.mp4").write_bytes(b"video")
            (test_source / "clip_1.json").write_text("{}", encoding="utf-8")
            (queue / "scheduler_state.json").write_text(json.dumps({"phase": "scheduled", "slot_index": 2}), encoding="utf-8")

            outcome = service.run_once()

            self.assertEqual("sourced", outcome.status)
            self.assertEqual([], service.fetch_calls)
            run_dirs = [d for d in queue.iterdir() if d.is_dir() and d.name != "source"]
            self.assertEqual(1, len(run_dirs))
            self.assertEqual(b"video", (run_dirs[0] / "source" / "clip_1.mp4").read_bytes())
            self.assertTrue((test_source / "clip_1.mp4").exists())
            state = json.loads((run_dirs[0] / "queue_state.json").read_text(encoding="utf-8"))
            self.assertEqual("sourced", state["phase"])
            self.assertEqual("source/clip_1.mp4", state["source_video"])
            scheduler = json.loads((queue / "scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual("source_consumed", scheduler["phase"])
            self.assertEqual(2, scheduler["slot_index"])

    def test_repeated_runs_create_new_run_folders(self) -> None:
        with TemporaryDirectory() as tmp:
            service, queue = self._service(Path(tmp))
            (queue / "source").mkdir(parents=True)
            (queue / "source" / "clip.mp4").write_bytes(b"v")
            service.run_once(bypass_schedule=True)
            service.run_once(bypass_schedule=True)
            self.assertEqual(2, len([d for d in queue.iterdir() if d.is_dir() and d.name != "source"]))

    def test_missing_test_source_reports_no_match_without_fetching(self) -> None:
        with TemporaryDirectory() as tmp:
            service, _ = self._service(Path(tmp))
            outcome = service.run_once(bypass_schedule=True)
            self.assertEqual("no_match", outcome.status)
            self.assertEqual([], service.fetch_calls)

class DownloadFallbackTests(unittest.TestCase):
    def test_test_only_helper_downloads_all_three_strategies(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(
                root,
                source_mode="profiles",
                source_profiles=["winner"],
                download_video_files=True,
                authorized_download_profiles=["winner"],
            )
            candidate = ClipCandidate(
                clip_id="winner",
                url="https://www.tiktok.com/@winner/video/1",
                text="sample clip",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
                author_handle="winner",
                hashtags=["sample"],
                plays=50000,
                likes=4000,
                shares=300,
                comments=120,
                saves=90,
                media_urls=[],
                raw_payload={"id": "winner"},
            )

            def write_strategy_file(*args) -> Path:
                destination_base = args[-1]
                path = destination_base.with_suffix(".mp4")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(destination_base.name.encode("utf-8"))
                return path

            with patch("clipsourcer.downloads.download_with_ytdlp", side_effect=write_strategy_file) as mock_ytdlp, patch(
                "clipsourcer.downloads.download_with_tikwm", side_effect=write_strategy_file
            ) as mock_tikwm, patch(
                "clipsourcer.downloads.download_with_apify", side_effect=write_strategy_file
            ) as mock_apify:
                result_paths = _download_all_video_files_for_test(config, candidate)

            self.assertEqual({"yt-dlp", "TikWM", "Apify"}, set(result_paths.keys()))
            self.assertEqual(3, len(result_paths))
            for strategy_name, path in result_paths.items():
                self.assertTrue(path.exists(), strategy_name)
                self.assertGreater(path.stat().st_size, 0, strategy_name)
            self.assertIn("winner", result_paths["yt-dlp"].stem)
            self.assertIn("winner", result_paths["TikWM"].stem)
            self.assertIn("winner", result_paths["Apify"].stem)
            mock_ytdlp.assert_called_once()
            mock_tikwm.assert_called_once()
            mock_apify.assert_called_once()

    def test_download_falls_back_to_tikwm_after_ytdlp_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(
                root,
                source_mode="profiles",
                source_profiles=["winner"],
                download_video_files=True,
                authorized_download_profiles=["winner"],
            )
            candidate = ClipCandidate(
                clip_id="winner",
                url="https://www.tiktok.com/@winner/video/1",
                text="sample clip",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
                author_handle="winner",
                hashtags=["sample"],
                plays=50000,
                likes=4000,
                shares=300,
                comments=120,
                saves=90,
                media_urls=[],
                raw_payload={"id": "winner"},
            )

            with patch("clipsourcer.downloads.download_with_ytdlp", side_effect=RuntimeError("yt-dlp failed")) as mock_ytdlp, patch(
                "clipsourcer.downloads.download_with_tikwm"
            ) as mock_tikwm, patch("clipsourcer.downloads.download_with_apify") as mock_apify:
                def write_tikwm_video(*_args) -> Path:
                    path = root / "queue" / "fallback.mp4"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"video-bytes")
                    return path

                mock_tikwm.side_effect = write_tikwm_video

                result_path = download_video_file(config, candidate)

            self.assertIsNotNone(result_path)
            assert result_path is not None
            self.assertTrue(result_path.exists())
            self.assertEqual(b"video-bytes", result_path.read_bytes())
            mock_ytdlp.assert_called_once()
            mock_tikwm.assert_called_once()
            mock_apify.assert_not_called()

    def test_download_cleans_up_failed_attempts_before_apify_fallback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(
                root,
                source_mode="profiles",
                source_profiles=["winner"],
                download_video_files=True,
                authorized_download_profiles=["winner"],
            )
            candidate = ClipCandidate(
                clip_id="winner",
                url="https://www.tiktok.com/@winner/video/1",
                text="sample clip",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
                author_handle="winner",
                hashtags=["sample"],
                plays=50000,
                likes=4000,
                shares=300,
                comments=120,
                saves=90,
                media_urls=[],
                raw_payload={"id": "winner"},
            )
            destination_base = build_clip_base_path(root / "queue", candidate)

            def fail_ytdlp(*_args) -> Path:
                destination_base.parent.mkdir(parents=True, exist_ok=True)
                leftover = destination_base.with_suffix(".part")
                leftover.write_bytes(b"partial")
                raise RuntimeError("yt-dlp failed")

            def fail_tikwm(*_args) -> Path:
                leftover = destination_base.with_suffix(".tmp")
                leftover.write_bytes(b"partial")
                raise RuntimeError("TikWM failed")

            def succeed_apify(*_args) -> Path:
                path = destination_base.with_suffix(".mp4")
                path.write_bytes(b"video-bytes")
                return path

            with patch("clipsourcer.downloads.download_with_ytdlp", side_effect=fail_ytdlp) as mock_ytdlp, patch(
                "clipsourcer.downloads.download_with_tikwm", side_effect=fail_tikwm
            ) as mock_tikwm, patch(
                "clipsourcer.downloads.download_with_apify", side_effect=succeed_apify
            ) as mock_apify:
                result_path = download_video_file(config, candidate)

            self.assertIsNotNone(result_path)
            assert result_path is not None
            self.assertTrue(result_path.exists())
            self.assertFalse(destination_base.with_suffix(".part").exists())
            self.assertFalse(destination_base.with_suffix(".tmp").exists())
            mock_ytdlp.assert_called_once()
            mock_tikwm.assert_called_once()
            mock_apify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
