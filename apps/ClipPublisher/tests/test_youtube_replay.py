import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import requests

from clippublisher import instagram_client
from clippublisher import runner as runner_module
from clippublisher import youtube_replay as replay_module
from clippublisher.config import AppConfig
from clippublisher.youtube_replay import GRAPH_API_VERSION, YouTubeReplay
from common.creators import Creator

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 14, 0, 30, tzinfo=NY)
YESTERDAY_NOON = datetime(2026, 10, 13, 12, 0, tzinfo=NY)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def views(value):
    return FakeResponse(200, {"data": [{"name": "views", "period": "lifetime", "values": [{"value": value}]}]})


def archive(queue, name, *, clip_id, media_id="", when=YESTERDAY_NOON, style="legacy", youtube_id="", video=True, status="success", folder="Archive"):
    run = queue / folder / name
    (run / "source").mkdir(parents=True)
    (run / "editor").mkdir()
    (run / "publisher").mkdir()
    (run / "source" / "clip.json").write_text(json.dumps({"clip_id": clip_id, "author_handle": "streamer", "text": f"text {clip_id}"}), encoding="utf-8")
    if video:
        (run / "editor" / "clip_edited.mp4").write_bytes(b"video")
    (run / "editor" / "youtube_description.txt").write_text(f"yt {clip_id}", encoding="utf-8")
    if style == "legacy":
        result = {"id": media_id}
    else:
        result = {}
        if media_id:
            result["instagram"] = {"id": media_id}
        if youtube_id:
            result["youtube"] = {"id": youtube_id, "url": f"https://youtube.com/shorts/{youtube_id}"}
    state = {"phase": "published", "status": status, "result": result, "errors": {}, "skipped": [], "updated_at": when.isoformat()}
    if style != "legacy" and media_id:
        state["published_at"] = {"instagram": when.isoformat()}
    (run / "publisher" / "publish_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "queue_state.json").write_text(json.dumps({"phase": "published"}), encoding="utf-8")
    return run


def make_config(queue):
    config = mock.MagicMock(spec=AppConfig)
    config.creators = ()
    config.queue_dir = queue
    config.timezone_name = "America/New_York"
    config.instagram_insights_api_version = GRAPH_API_VERSION
    config.youtube_selection_mode = "instagram_top"
    config.youtube_posts_per_day = 10
    config.youtube_start_hour = 0
    config.youtube_end_hour = 24
    config.replay_profile = "test"
    config.instagram_user_id = "ig-account"
    return config


def slots(*hours):
    return [datetime(2026, 10, 14, hour, 0, tzinfo=NY) for hour in hours]


class ReplayTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name) / "Queue"
        self.queue.mkdir()
        self.replay = YouTubeReplay(make_config(self.queue))
        self.views = {}
        self.get = mock.Mock(side_effect=self._fake_get)
        self.token = mock.Mock(return_value="ig-token")
        self.upload = mock.Mock(side_effect=lambda config, item, **kwargs: {"id": f"yt-{item.clip_id}", "url": f"https://youtube.com/shorts/yt-{item.clip_id}"})
        self.posted = mock.Mock()
        self.failed = mock.Mock()
        self.screen = mock.Mock()
        self.ig = mock.Mock(side_effect=AssertionError("Instagram must never be called"))
        forbidden = mock.Mock(side_effect=AssertionError("unexpected live HTTP call"))
        for patcher in [
            mock.patch.object(replay_module.requests, "get", self.get),
            mock.patch.object(requests, "post", forbidden),
            mock.patch.object(requests, "put", forbidden),
            mock.patch.object(replay_module, "resolve_publish_access_token", self.token),
            mock.patch.object(replay_module, "publish_to_youtube", self.upload),
            mock.patch.object(replay_module, "screen_youtube_content", self.screen),
            mock.patch.object(replay_module, "youtube_is_configured", lambda config: True),
            mock.patch.object(replay_module, "send_posted_alert", self.posted),
            mock.patch.object(replay_module, "send_failure_alert", self.failed),
            mock.patch.object(instagram_client, "publish_to_instagram", self.ig),
            mock.patch.object(runner_module, "publish_to_instagram", self.ig),
            mock.patch.object(runner_module.QueuePublisherRunner, "_record_seen_id", mock.Mock(side_effect=AssertionError("seen_ids must not change"))),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_get(self, url, params=None, timeout=None):
        self.assertEqual("views", params["metric"])
        self.assertEqual("ig-token", params["access_token"])
        self.assertIn(f"graph.facebook.com/{GRAPH_API_VERSION}/", url)
        media_id = url.split("/")[-2]
        value = self.views[media_id]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, FakeResponse) else views(value)

    def plan(self):
        return json.loads((self.queue / "youtube_plan.json").read_text(encoding="utf-8"))

    def history(self):
        path = self.queue / "youtube_history.json"
        return json.loads(path.read_text(encoding="utf-8"))["entries"] if path.exists() else []


class PrepareTests(ReplayTestCase):
    def creator_archives(self):
        self.replay.config.creators = (
            Creator("one", 2, 1, {"STREAMER_NAME": "One"}),
            Creator("two", 2, 1, {"STREAMER_NAME": "Two"}),
        )
        self.replay.config.youtube_posts_per_day = 2
        for creator, scores in (("one", (100, 80)), ("two", (1000, 900))):
            for suffix, score in zip(("a", "b"), scores):
                name = creator + suffix
                run = archive(self.queue, name, clip_id=name, media_id=name)
                (run / "creator.json").write_text(json.dumps({
                    "name": creator, "display_name": creator.title(),
                    "settings": {"STREAMER_HASHTAG": creator},
                }))
                self.views[name] = score

    def test_creator_youtube_quotas_do_not_allow_global_top_clips_to_take_over(self):
        self.creator_archives()
        self.assertTrue(self.replay.prepare(NOW, slots(9, 10)))
        entries = self.plan()["entries"]
        self.assertEqual({entry["clip_id"] for entry in entries}, {"onea", "twoa"})
        self.assertEqual({entry["creator_name"] for entry in entries}, {"One", "Two"})

    def test_creator_without_eligible_posts_does_not_donate_quota(self):
        self.creator_archives()
        for name in ("onea", "oneb"):
            (self.queue / "Archive" / name / "publisher" / "publish_state.json").unlink()
        self.assertTrue(self.replay.prepare(NOW, slots(9, 10)))
        self.assertEqual([entry["clip_id"] for entry in self.plan()["entries"]], ["twoa"])

    def test_rejected_creator_slot_uses_its_own_reserve_even_when_other_creator_has_more_views(self):
        self.creator_archives()
        with mock.patch.object(replay_module.random.SystemRandom, "shuffle", side_effect=lambda rows: rows.sort(key=lambda row: row["creator"])):
            self.assertTrue(self.replay.prepare(NOW, slots(9, 10)))

        def screen(config, item):
            if item.clip_id == "onea":
                raise replay_module.YouTubePolicyError("AI rejected this clip")

        self.screen.side_effect = screen
        self.assertTrue(self.replay.process_due(NOW.replace(hour=9)))
        self.assertEqual(self.upload.call_args.args[1].clip_id, "oneb")

    def test_ai_screen_excludes_transcript_violation_and_uses_next_highest_views(self):
        risky = archive(self.queue, "risky", clip_id="risky", media_id="m-risky")
        (risky / "ai").mkdir()
        (risky / "ai" / "transcript.txt").write_text("Send me your password for a free reward.", encoding="utf-8")
        archive(self.queue, "next", clip_id="next", media_id="m-next")
        archive(self.queue, "third", clip_id="third", media_id="m-third")
        self.views = {"m-risky": 1000, "m-next": 800, "m-third": 700}

        def screen(config, item):
            if item.clip_id == "risky":
                self.assertIn("Send me your password", item.transcript)
                raise replay_module.YouTubePolicyError("AI identified credential phishing.")

        self.screen.side_effect = screen
        self.assertTrue(self.replay.prepare(NOW, slots(9)))

        plan = self.plan()
        self.assertEqual(["next"], [entry["clip_id"] for entry in plan["entries"]])
        self.assertEqual(["third"], plan["unscheduled"])
        self.assertIn("youtube_ai_policy", plan["excluded"][0]["reason"])
        self.assertIn("credential phishing", plan["excluded"][0]["reason"])
        fetched = {call.args[0].split("/")[-2] for call in self.get.call_args_list}
        self.assertNotIn("m-risky", fetched)
        self.failed.assert_called_once()
        alert = self.failed.call_args.kwargs
        self.assertEqual("YouTube", alert["platform"])
        self.assertEqual("Rejected - risky", alert["subject"])
        self.assertIn("credential phishing", str(alert["error"]))
        self.assertIn("next-highest-view", alert["details"])

    def test_simulation_does_not_send_policy_rejection_emails(self):
        archive(self.queue, "a", clip_id="a", media_id="ma")
        self.screen.side_effect = replay_module.YouTubePolicyError("AI found a policy concern.")
        with mock.patch.object(replay_module, "read_access_token", return_value="ig-token"):
            plan = self.replay.simulate(NOW, slots(9))
        self.assertEqual([], plan["entries"])
        self.failed.assert_not_called()

    def test_policy_alert_failure_does_not_prevent_next_candidate_selection(self):
        archive(self.queue, "a", clip_id="a", media_id="ma")
        archive(self.queue, "b", clip_id="b", media_id="mb")
        self.views = {"mb": 20}
        def screen(config, item):
            if item.clip_id == "a":
                raise replay_module.YouTubePolicyError("AI found a policy concern.")

        self.screen.side_effect = screen
        self.failed.side_effect = RuntimeError("SMTP unavailable")
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertTrue(self.replay.prepare(NOW, slots(9)))
        self.assertEqual("b", self.plan()["entries"][0]["clip_id"])

    def test_ai_outage_does_not_finalize_plan_or_mark_clips_as_violations(self):
        archive(self.queue, "a", clip_id="a", media_id="ma")
        self.views = {"ma": 100}
        self.screen.side_effect = replay_module.YouTubeScreeningError("AI unavailable")
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.prepare(NOW, slots(9)))
        self.assertFalse(self.replay.plan_path.exists())
        self.assertEqual([], self.history())
        self.upload.assert_not_called()
        self.failed.assert_not_called()
        self.screen.side_effect = None
        self.assertTrue(self.replay.prepare(NOW, slots(9)))
        self.assertEqual(["a"], [entry["clip_id"] for entry in self.plan()["entries"]])

    def test_insights_version_is_configurable_and_publish_version_is_unchanged(self):
        config = make_config(self.queue)
        config.instagram_insights_api_version = "v23.0"
        replay = YouTubeReplay(config)
        self.get.side_effect = None
        self.get.return_value = views(7)
        self.assertEqual(7, replay.fetch_views("m1", "ig-token"))
        self.assertEqual("https://graph.facebook.com/v23.0/m1/insights", self.get.call_args.args[0])
        for version in ("v20.0", "v22.0", "latest", "v24.0/other"):
            config.instagram_insights_api_version = version
            with self.assertRaises(ValueError):
                YouTubeReplay(config)

    def test_no_yesterday_posts_gives_empty_plan_without_api_calls(self):
        archive(self.queue, "old", clip_id="old", media_id="m-old", when=YESTERDAY_NOON - timedelta(days=1))
        self.assertTrue(self.replay.prepare(NOW, slots(9, 15)))
        plan = self.plan()
        self.assertEqual("complete", plan["status"])
        self.assertEqual("2026-10-13", plan["source_date"])
        self.assertEqual([], plan["entries"])
        self.get.assert_not_called()
        self.token.assert_not_called()

    def test_missing_queue_state_files_is_an_empty_plan(self):
        self.assertTrue(self.replay.prepare(NOW, slots(9)))
        self.assertEqual([], self.plan()["entries"])
        self.get.assert_not_called()

    def test_yesterday_uses_configured_timezone_boundaries(self):
        archive(self.queue, "a", clip_id="late", media_id="m1", when=datetime(2026, 10, 13, 23, 59, tzinfo=NY), style="nested")
        archive(self.queue, "b", clip_id="today", media_id="m2", when=datetime(2026, 10, 14, 0, 10, tzinfo=NY), style="nested")
        archive(self.queue, "c", clip_id="utc-before", media_id="m3", when=datetime(2026, 10, 13, 3, 30, tzinfo=timezone.utc))
        archive(self.queue, "d", clip_id="utc-early", media_id="m4", when=datetime(2026, 10, 13, 4, 30, tzinfo=timezone.utc))
        self.views = {"m1": 10, "m4": 20}
        # The scheduler may pass UTC; "today" is still evaluated in the publisher timezone.
        self.assertTrue(self.replay.prepare(NOW.astimezone(timezone.utc), slots(9, 10, 11)))
        self.assertEqual(["utc-early", "late"], [entry["clip_id"] for entry in self.plan()["entries"]])
        self.assertEqual({"m1", "m4"}, {call.args[0].split("/")[-2] for call in self.get.call_args_list})

    def test_per_platform_timestamp_is_preferred_over_updated_at(self):
        run = archive(self.queue, "a", clip_id="c1", media_id="m1", style="nested", when=datetime(2026, 10, 14, 0, 5, tzinfo=NY))
        state_path = run / "publisher" / "publish_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["published_at"] = {"instagram": YESTERDAY_NOON.isoformat()}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.views = {"m1": 5}
        self.assertTrue(self.replay.prepare(NOW, slots(9)))
        self.assertEqual(["c1"], [entry["clip_id"] for entry in self.plan()["entries"]])

    def test_ranks_by_views_with_deterministic_ties_exclusions_and_cap(self):
        archive(self.queue, "r1", clip_id="low", media_id="m-low")
        archive(self.queue, "r2", clip_id="tie-b", media_id="m-tb", style="nested")
        archive(self.queue, "r3", clip_id="tie-a", media_id="m-ta", style="nested")
        archive(self.queue, "r4", clip_id="tie-early", media_id="m-te", when=YESTERDAY_NOON - timedelta(hours=1))
        archive(self.queue, "r5", clip_id="top", media_id="m-top", folder="processing")
        archive(self.queue, "r6", clip_id="both", media_id="m-both", style="nested", youtube_id="yt-already")
        archive(self.queue, "r7", clip_id="novideo", media_id="m-nv", video=False)
        archive(self.queue, "r8", clip_id="history", media_id="m-h")
        archive(self.queue, "r9", clip_id="yt-only", style="nested", youtube_id="yt2", status="partial")
        archive(self.queue, "r10_failed", clip_id="failed", media_id="m-f", status="error")
        archive(self.queue, "r11", clip_id="partial-ig", media_id="m-p", style="nested", status="partial")
        archive(self.queue, "r12", clip_id="top", media_id="m-top-dup")
        (self.queue / "youtube_history.json").write_text(json.dumps({"version": 1, "entries": [{"clip_id": "history", "status": "published"}]}), encoding="utf-8")
        self.views = {"m-low": 1, "m-tb": 50, "m-ta": 50, "m-te": 50, "m-top": 900, "m-top-dup": 100, "m-p": 7}

        self.assertTrue(self.replay.prepare(NOW, list(reversed(slots(9, 12, 15, 18)))))
        plan = self.plan()
        self.assertEqual(["top", "tie-early", "tie-a", "tie-b"], [entry["clip_id"] for entry in plan["entries"]])
        self.assertEqual([900, 50, 50, 50], [entry["views"] for entry in plan["entries"]])
        self.assertEqual("m-top", plan["entries"][0]["media_id"])
        self.assertEqual([entry["slot"] for entry in plan["entries"]], sorted(entry["slot"] for entry in plan["entries"]))
        self.assertEqual(["partial-ig", "low"], plan["unscheduled"])
        reasons = {entry["clip_id"]: entry["reason"] for entry in plan["excluded"]}
        self.assertEqual({"both": "already_on_youtube", "novideo": "missing_local_media", "history": "already_on_youtube"}, reasons)
        fetched = {call.args[0].split("/")[-2] for call in self.get.call_args_list}
        self.assertNotIn("m-both", fetched)
        self.assertNotIn("m-nv", fetched)
        self.assertNotIn("m-f", fetched)

    def test_simulate_ranks_like_prepare_but_writes_nothing_and_never_refreshes(self):
        archive(self.queue, "r1", clip_id="low", media_id="m-low")
        archive(self.queue, "r2", clip_id="top", media_id="m-top")
        self.views = {"m-low": 3, "m-top": 30}
        before = sorted(str(path) for path in self.queue.rglob("*"))
        with mock.patch.object(replay_module, "read_access_token", return_value="ig-token") as read_token:
            plan = self.replay.simulate(NOW, slots(9))
        read_token.assert_called_once()
        self.token.assert_not_called()
        self.upload.assert_not_called()
        self.assertEqual(["top"], [entry["clip_id"] for entry in plan["entries"]])
        self.assertEqual(["low"], plan["unscheduled"])
        self.assertEqual(before, sorted(str(path) for path in self.queue.rglob("*")))
        self.assertTrue(self.replay.prepare(NOW, slots(9)))
        self.assertEqual(plan["entries"], self.plan()["entries"])

    def test_simulate_missing_views_returns_none_without_writing(self):
        archive(self.queue, "a", clip_id="x", media_id="m1")
        self.views = {"m1": FakeResponse(400, {"error": {"code": 10, "message": "no permission"}})}
        with mock.patch.object(replay_module, "read_access_token", return_value="ig-token"), \
             self.assertLogs(replay_module.logger, "ERROR"):
            self.assertIsNone(self.replay.simulate(NOW, slots(9)))
        self.assertFalse(self.replay.plan_path.exists())

    def test_missing_views_metric_blocks_partial_ranking_without_fallback(self):
        archive(self.queue, "a", clip_id="ok", media_id="m1")
        archive(self.queue, "b", clip_id="nometric", media_id="m2")
        archive(self.queue, "c", clip_id="unsupported", media_id="m3")
        self.views = {
            "m1": 3,
            "m2": FakeResponse(200, {"data": []}),
            "m3": FakeResponse(400, {"error": {"code": 100, "message": "metric not supported"}}),
        }
        with self.assertLogs(replay_module.logger, "ERROR") as logs:
            self.assertFalse(self.replay.prepare(NOW, slots(9, 10, 11)))
        self.assertFalse(self.replay.plan_path.exists())
        self.assertTrue(any("nometric" in line for line in logs.output))
        self.views["m2"] = 10
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.prepare(NOW, slots(9, 10, 11)))
        self.assertFalse(self.replay.plan_path.exists())
        self.views["m3"] = 20
        self.assertTrue(self.replay.prepare(NOW, slots(9, 10, 11)))
        self.assertEqual(["unsupported", "nometric", "ok"], [entry["clip_id"] for entry in self.plan()["entries"]])

    def test_all_missing_views_retries_instead_of_completing_empty_snapshot(self):
        archive(self.queue, "a", clip_id="a", media_id="ma")
        self.views = {"ma": FakeResponse(200, {"data": []})}
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.prepare(NOW, slots(11)))
        self.assertFalse(self.replay.plan_path.exists())
        self.views["ma"] = 0
        self.assertTrue(self.replay.prepare(NOW, slots(11)))
        self.assertEqual(0, self.plan()["entries"][0]["views"])

    def test_transient_insights_error_leaves_plan_unfinished_then_retry_succeeds(self):
        archive(self.queue, "a", clip_id="c1", media_id="m1")
        archive(self.queue, "b", clip_id="c2", media_id="m2")
        transients = (
            FakeResponse(500, {"error": {"code": 2, "message": "service"}}),
            FakeResponse(400, {"error": {"code": 4, "message": "rate"}}),
            requests.ConnectionError("down"),
            FakeResponse(400, {"error": {"code": 190, "message": "expired"}}),
            FakeResponse(400, {"error": {"code": 10, "message": "permission denied"}}),
            FakeResponse(403, {"error": {"code": 200, "message": "permission denied"}}),
        )
        for transient in transients:
            self.views = {"m1": 10, "m2": transient}
            with self.assertLogs(replay_module.logger, "ERROR"):
                self.assertFalse(self.replay.prepare(NOW, slots(9, 10)))
            self.assertFalse((self.queue / "youtube_plan.json").exists())
        self.views = {"m1": 10, "m2": 20}
        self.assertTrue(self.replay.prepare(NOW, slots(9, 10)))
        self.assertEqual([20, 10], [entry["views"] for entry in self.plan()["entries"]])

    def test_token_failure_returns_false(self):
        archive(self.queue, "a", clip_id="c1", media_id="m1")
        self.token.side_effect = ValueError("INSTAGRAM_ACCESS_TOKEN is required")
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.prepare(NOW, slots(9)))
        self.assertFalse((self.queue / "youtube_plan.json").exists())
        self.get.assert_not_called()

    def test_same_day_restart_never_reranks_completed_snapshot(self):
        archive(self.queue, "a", clip_id="c1", media_id="m1")
        archive(self.queue, "b", clip_id="c2", media_id="m2")
        self.views = {"m1": 10, "m2": 20}
        self.assertTrue(self.replay.prepare(NOW, slots(9, 10)))
        first = self.plan()
        self.get.reset_mock()
        self.views = {"m1": 999, "m2": 1}
        restarted = YouTubeReplay(make_config(self.queue))
        self.assertTrue(restarted.prepare(NOW + timedelta(hours=5), slots(9, 10, 11)))
        self.assertEqual(first, self.plan())
        self.get.assert_not_called()

    def test_new_day_replaces_previous_plan(self):
        archive(self.queue, "a", clip_id="c1", media_id="m1")
        self.views = {"m1": 10}
        self.assertTrue(self.replay.prepare(NOW, slots(9)))
        self.assertTrue(self.replay.prepare(NOW + timedelta(days=1), [slot + timedelta(days=1) for slot in slots(9)]))
        plan = self.plan()
        self.assertEqual("2026-10-15", plan["date"])
        self.assertEqual([], plan["entries"])

    def test_corrupt_plan_is_backed_up_and_rebuilt(self):
        archive(self.queue, "a", clip_id="c1", media_id="m1")
        self.views = {"m1": 10}
        (self.queue / "youtube_plan.json").write_text("{not json", encoding="utf-8")
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertTrue(self.replay.prepare(NOW, slots(9)))
        self.assertEqual(["c1"], [entry["clip_id"] for entry in self.plan()["entries"]])
        self.assertEqual(1, len(list(self.queue.glob("youtube_plan.json.invalid-*"))))

    def test_atomic_write_leaves_previous_plan_on_failure(self):
        self.assertTrue(self.replay.prepare(NOW, slots(9)))
        before = (self.queue / "youtube_plan.json").read_text(encoding="utf-8")
        with mock.patch.object(replay_module.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                replay_module._atomic_write_json(self.queue / "youtube_plan.json", {"x": 1})
        self.assertEqual(before, (self.queue / "youtube_plan.json").read_text(encoding="utf-8"))
        self.assertEqual([], list(self.queue.glob(".youtube_plan.json.*")))

    def test_requires_aware_datetimes(self):
        with self.assertRaises(ValueError):
            self.replay.prepare(NOW.replace(tzinfo=None), slots(9))
        with self.assertRaises(ValueError):
            self.replay.prepare(NOW, [datetime(2026, 10, 14, 9)])
        with self.assertRaises(ValueError):
            self.replay.process_due(NOW.replace(tzinfo=None))


class ProcessDueTests(ReplayTestCase):
    def test_publish_time_ai_rejection_uses_next_highest_reserve_without_spending_quota(self):
        for clip_id in ("a", "b", "c"):
            archive(self.queue, f"run_{clip_id}", clip_id=clip_id, media_id=f"m{clip_id}")
        self.views = {"ma": 30, "mb": 20, "mc": 10}
        self.replay.config.youtube_posts_per_day = 1
        self.assertTrue(self.replay.prepare(NOW, slots(9)))

        def screen(config, item):
            if item.clip_id in ("a", "b"):
                raise replay_module.YouTubePolicyError("AI found targeted harassment.")

        self.screen.side_effect = screen
        self.assertTrue(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.upload.assert_called_once()
        self.assertEqual("c", self.upload.call_args.args[1].clip_id)
        plan = self.plan()
        self.assertEqual("c", plan["entries"][0]["clip_id"])
        self.assertEqual(slots(9)[0].isoformat(), plan["entries"][0]["slot"])
        self.assertEqual(["a", "b"], [entry["clip_id"] for entry in plan["excluded"]])
        self.assertEqual(["c"], [entry["clip_id"] for entry in self.history()])
        self.assertEqual(2, self.failed.call_count)
        self.assertEqual(["a", "b"], [call.kwargs["clip_id"] for call in self.failed.call_args_list])
        self.assertIn("targeted harassment", str(self.failed.call_args.kwargs["error"]))
        self.assertIn("reserve: c (10 Instagram views)", self.failed.call_args.kwargs["details"])
        restarted = YouTubeReplay(self.replay.config)
        self.assertFalse(restarted.process_due(datetime(2026, 10, 14, 9, 2, tzinfo=NY)))

    def test_publish_time_ai_outage_leaves_slot_pending_without_attempt(self):
        self.prepared()
        self.screen.side_effect = replay_module.YouTubeScreeningError("AI unavailable")
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.assertEqual("pending", self.plan()["entries"][0]["status"])
        self.assertNotIn("attempted_at", self.plan()["entries"][0])
        self.assertEqual([], self.history())
        self.upload.assert_not_called()
        self.failed.assert_not_called()
        self.screen.side_effect = None
        self.assertTrue(self.replay.process_due(datetime(2026, 10, 14, 9, 2, tzinfo=NY)))

    def test_publish_time_ai_rejection_without_reserves_does_not_upload(self):
        archive(self.queue, "run_a", clip_id="a", media_id="ma")
        self.views = {"ma": 30}
        self.assertTrue(self.replay.prepare(NOW, slots(9)))
        self.screen.side_effect = replay_module.YouTubePolicyError("AI found a policy violation.")
        self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.assertEqual("failed", self.plan()["entries"][0]["status"])
        self.assertEqual([], self.history())
        self.upload.assert_not_called()
        self.failed.assert_called_once()
        self.assertIn("No eligible reserve", self.failed.call_args.kwargs["details"])

    def prepared(self):
        archive(self.queue, "run_a", clip_id="a", media_id="ma")
        archive(self.queue, "run_b", clip_id="b", media_id="mb")
        archive(self.queue, "run_c", clip_id="c", media_id="mc")
        self.views = {"ma": 30, "mb": 20, "mc": 10}
        self.assertTrue(self.replay.prepare(NOW, slots(9, 12, 15)))

    def test_nothing_due_or_no_plan(self):
        self.assertFalse(self.replay.process_due(NOW))
        self.prepared()
        self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 8, 59, tzinfo=NY)))
        self.upload.assert_not_called()

    def test_uploads_due_clip_to_youtube_only_with_archived_metadata(self):
        self.prepared()
        history_at_notify = []
        self.posted.side_effect = lambda **kwargs: history_at_notify.append(self.history())
        self.assertTrue(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.upload.assert_called_once()
        item = self.upload.call_args.args[1]
        self.assertEqual("a", item.clip_id)
        self.assertEqual(self.queue / "Archive" / "run_a" / "editor" / "clip_edited.mp4", item.video_path)
        self.assertEqual("yt a", item.youtube_description)
        self.ig.assert_not_called()
        self.assertEqual("youtube", self.posted.call_args.kwargs["platform"])
        self.assertEqual("published", history_at_notify[0][0]["status"])
        self.assertEqual("yt-a", history_at_notify[0][0]["youtube"]["id"])
        plan = self.plan()
        self.assertEqual(["published", "pending", "pending"], [entry["status"] for entry in plan["entries"]])
        self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 9, 2, tzinfo=NY)))
        self.upload.assert_called_once()

    def test_attempt_is_persisted_before_upload(self):
        self.prepared()
        seen = []

        def upload(config, item, **kwargs):
            seen.append(([entry["status"] for entry in self.plan()["entries"]], self.history()[0]["status"]))
            return {"id": "yt-a"}

        self.upload.side_effect = upload
        self.assertTrue(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.assertEqual([(["attempted", "pending", "pending"], "attempted")], seen)

    def test_crash_during_upload_is_never_retried_after_restart(self):
        self.prepared()
        self.upload.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY))
        self.upload.reset_mock(side_effect=True)
        restarted = YouTubeReplay(make_config(self.queue))
        self.assertTrue(restarted.prepare(datetime(2026, 10, 14, 9, 5, tzinfo=NY), slots(9, 12, 15)))
        self.assertFalse(restarted.process_due(datetime(2026, 10, 14, 9, 6, tzinfo=NY)))
        self.upload.assert_not_called()
        self.assertEqual("attempted", self.plan()["entries"][0]["status"])

    def test_failure_alerts_once_and_later_slots_continue(self):
        self.prepared()
        self.upload.side_effect = [RuntimeError("quota exceeded"), {"id": "yt-b", "url": "u"}]
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.failed.assert_called_once()
        self.assertEqual("YouTube", self.failed.call_args.kwargs["platform"])
        self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 11, 0, tzinfo=NY)))
        self.assertTrue(self.replay.process_due(datetime(2026, 10, 14, 12, 0, tzinfo=NY)))
        self.assertEqual(2, self.upload.call_count)
        statuses = {entry["clip_id"]: entry["status"] for entry in self.plan()["entries"]}
        self.assertEqual({"a": "failed", "b": "published", "c": "pending"}, statuses)
        history = {entry["clip_id"]: entry for entry in self.history()}
        self.assertEqual("failed", history["a"]["status"])
        self.assertIn("quota", history["a"]["error"])
        self.assertEqual("published", history["b"]["status"])

    def test_missing_archive_media_fails_without_upload(self):
        self.prepared()
        (self.queue / "Archive" / "run_a" / "editor" / "clip_edited.mp4").unlink()
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.upload.assert_not_called()
        self.failed.assert_called_once()
        self.assertEqual("failed", self.plan()["entries"][0]["status"])

    def test_unconfigured_youtube_is_an_explicit_failure(self):
        self.prepared()
        with mock.patch.object(replay_module, "youtube_is_configured", lambda config: False), self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.upload.assert_not_called()
        self.failed.assert_called_once()

    def test_duplicate_in_history_is_not_uploaded(self):
        self.prepared()
        (self.queue / "youtube_history.json").write_text(json.dumps({"version": 1, "entries": [{"clip_id": "a", "status": "published"}]}), encoding="utf-8")
        with self.assertLogs(replay_module.logger, "ERROR"):
            self.assertFalse(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.upload.assert_not_called()
        self.assertEqual("failed", self.plan()["entries"][0]["status"])

    def test_notification_failure_does_not_undo_success(self):
        self.prepared()
        self.posted.side_effect = RuntimeError("smtp down")
        self.assertTrue(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        self.assertEqual("published", self.history()[0]["status"])
        self.assertEqual("published", self.plan()["entries"][0]["status"])

    def test_published_clip_is_excluded_from_next_days_ranking(self):
        self.prepared()
        self.assertTrue(self.replay.process_due(datetime(2026, 10, 14, 9, 1, tzinfo=NY)))
        # The same clip posted to IG again the next day must not be replayed twice.
        archive(self.queue, "run_a_again", clip_id="a", media_id="ma2", when=datetime(2026, 10, 14, 12, 0, tzinfo=NY))
        self.views = {"ma2": 999}
        self.assertTrue(self.replay.prepare(NOW + timedelta(days=1), [datetime(2026, 10, 15, 9, 0, tzinfo=NY)]))
        plan = self.plan()
        self.assertEqual([], plan["entries"])
        self.assertEqual("already_on_youtube", plan["excluded"][0]["reason"])

    def test_plan_from_another_day_is_ignored(self):
        self.prepared()
        self.assertFalse(self.replay.process_due(datetime(2026, 10, 15, 9, 1, tzinfo=NY)))
        self.upload.assert_not_called()

    def test_window_end_skips_missed_clips_without_upload_or_attempt(self):
        self.replay.config.youtube_start_hour = 10
        self.replay.config.youtube_end_hour = 12
        archive(self.queue, "a", clip_id="a", media_id="ma")
        self.views = {"ma": 30}
        self.assertTrue(self.replay.prepare(NOW, slots(11)))
        self.assertFalse(self.replay.process_due(slots(9)[0]))
        self.assertFalse(self.replay.process_due(slots(12)[0]))
        self.assertFalse(self.replay.process_due(slots(18)[0]))
        self.upload.assert_not_called()
        self.assertEqual("failed", self.plan()["entries"][0]["status"])
        self.assertIn("missed_window", self.plan()["entries"][0]["error"])
        self.assertEqual([], self.history())

    def test_disabled_quota_and_shared_mode_block_existing_plan(self):
        self.prepared()
        self.replay.config.youtube_posts_per_day = 0
        self.assertFalse(self.replay.process_due(slots(9)[0]))
        self.replay.config.youtube_posts_per_day = 1
        self.replay.config.youtube_selection_mode = "shared"
        self.assertFalse(self.replay.process_due(slots(9)[0]))
        self.upload.assert_not_called()

    def test_quota_caps_preparation_and_existing_plan_attempts(self):
        self.prepared()
        self.replay.config.youtube_posts_per_day = 1
        self.assertTrue(self.replay.process_due(slots(9)[0]))
        self.assertFalse(self.replay.process_due(slots(15)[0]))
        self.upload.assert_called_once()
        self.replay.plan_path.unlink()
        self.assertTrue(self.replay.prepare(NOW, slots(9, 12, 15)))
        self.assertEqual(1, len(self.plan()["entries"]))
        self.assertFalse(self.replay.process_due(slots(15)[0]))
        self.upload.assert_called_once()

    def test_other_profile_or_account_snapshot_is_blocked_not_reranked(self):
        self.prepared()
        before = self.plan()
        for field in ("replay_profile", "instagram_user_id"):
            old = getattr(self.replay.config, field)
            setattr(self.replay.config, field, "another")
            with self.assertLogs(replay_module.logger, "ERROR"):
                self.assertFalse(self.replay.process_due(slots(9)[0]))
                self.assertFalse(self.replay.prepare(NOW, slots(9)))
            setattr(self.replay.config, field, old)
        self.assertEqual(before, self.plan())
        self.upload.assert_not_called()

    def test_alert_entry_shape_and_legacy_snapshot_remain_usable(self):
        self.prepared()
        plan = self.plan()
        self.assertEqual("pending", plan["entries"][0]["status"])
        self.assertEqual(plan["entries"][0]["slot"], plan["entries"][0]["scheduled_for"])
        for entry in plan["entries"]:
            entry.pop("scheduled_for")
            entry["status"] = "planned"
        self.replay._save_plan(plan)
        self.get.reset_mock()
        self.assertTrue(self.replay.prepare(NOW, slots(9, 12, 15)))
        self.assertEqual(plan, self.plan())
        self.get.assert_not_called()
        self.assertTrue(self.replay.process_due(slots(9)[0]))
        self.assertEqual(plan["entries"][0]["slot"], self.history()[0]["scheduled_for"])

    def test_replay_uses_archived_ai_summary_without_youtube_metadata(self):
        from clippublisher.queue import build_queue_item
        from clippublisher.youtube_client import build_title

        self.prepared()
        run = self.queue / "Archive" / "run_a"
        (run / "editor" / "youtube_description.txt").unlink()
        (run / "ai").mkdir()
        summary = "Archived AI highlight.\n\nMore detail.\n\n#clutch"
        (run / "ai" / "summary.txt").write_text(summary, encoding="utf-8")
        self.assertTrue(self.replay.process_due(slots(9)[0]))
        item = self.upload.call_args.args[1]
        self.assertEqual(summary, item.description)
        self.assertEqual("", item.youtube_description)
        config = make_config(self.queue)
        config.streamer_hashtag = ""
        self.assertEqual("Archived AI highlight. #clutch", build_title(config, item))
        self.assertEqual("", build_queue_item(run).description)
        (run / "editor" / "description.txt").write_text("Preferred editor metadata", encoding="utf-8")
        self.assertEqual("Preferred editor metadata", build_queue_item(run, use_ai_summary=True).description)


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_youtube import make_runner

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue, self.runner, self.seen = make_runner(self._tmp.name, ["instagram", "youtube"])
        self.runner.config.instagram_insights_api_version = GRAPH_API_VERSION
        self.runner.config.youtube_start_hour = 10
        self.runner.config.youtube_end_hour = 12
        self.replay_cls = mock.Mock()
        self.replay_cls.return_value.process_due.return_value = True
        self.ig = mock.Mock(return_value={"id": "ig1"})
        self.yt = mock.Mock(return_value={"id": "yt1", "url": "u"})
        env = {key: value for key, value in os.environ.items() if key not in {"YOUTUBE_SELECTION_MODE", "CLIPPER_TEST_MODE"}}
        for patcher in [
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(replay_module, "YouTubeReplay", self.replay_cls),
            mock.patch.object(runner_module, "publish_to_instagram", self.ig),
            mock.patch.object(runner_module, "publish_to_youtube", self.yt),
            mock.patch.object(runner_module, "youtube_is_configured", lambda c: True),
            mock.patch.object(runner_module, "resolve_publish_access_token", lambda c: "t"),
            mock.patch.object(runner_module, "send_failure_alert"),
            mock.patch.object(runner_module, "send_email_alert"),
            mock.patch.object(self.runner, "_sourcer_seen_ids_path", lambda: self.seen),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_shared_mode_never_touches_replay(self):
        self.assertTrue(self.runner.process_one_ready_item())
        self.replay_cls.assert_not_called()
        self.yt.assert_called_once()

    def test_instagram_top_runs_replay_and_keeps_youtube_out_of_shared_queue(self):
        with mock.patch.dict("os.environ", {"YOUTUBE_SELECTION_MODE": "instagram_top"}):
            self.assertTrue(self.runner.process_one_ready_item())
            self.assertFalse(self.runner.process_one_ready_item())
        self.replay_cls.assert_called_once_with(self.runner.config)
        self.assertEqual(2, self.replay_cls.return_value.process_due.call_count)
        self.assertIsNotNone(self.replay_cls.return_value.process_due.call_args.args[0].tzinfo)
        self.ig.assert_called_once()
        self.yt.assert_not_called()
        state = json.loads((self.queue / "Archive" / "0001_x" / "publisher" / "publish_state.json").read_text(encoding="utf-8"))
        self.assertEqual(["instagram"], list(state["published_at"]))
        self.assertEqual("ig1", replay_module.instagram_media_id(state["result"]))

    def test_replay_disabled_in_test_mode(self):
        with mock.patch.dict("os.environ", {"YOUTUBE_SELECTION_MODE": "instagram_top", "CLIPPER_TEST_MODE": "1"}):
            self.assertFalse(self.runner.process_youtube_replay())
        self.replay_cls.assert_not_called()

    def test_replay_errors_do_not_block_the_queue(self):
        self.replay_cls.return_value.process_due.side_effect = RuntimeError("bad plan")
        with mock.patch.dict("os.environ", {"YOUTUBE_SELECTION_MODE": "instagram_top"}), self.assertLogs(runner_module.logger, "ERROR"):
            self.assertTrue(self.runner.process_one_ready_item())
        self.ig.assert_called_once()

    def test_run_forever_polls_replay(self):
        with mock.patch.dict("os.environ", {"YOUTUBE_SELECTION_MODE": "instagram_top"}), \
             mock.patch.object(runner_module.time, "sleep", side_effect=KeyboardInterrupt):
            self.runner.run_forever()
        self.replay_cls.return_value.process_due.assert_called_once()
        self.ig.assert_called_once()
        self.yt.assert_not_called()

    def test_queue_discovery_ignores_replay_files(self):
        from clippublisher.queue import discover_queue_items

        for name in ("youtube_plan.json", "youtube_history.json", ".youtube_plan.json.abc.tmp", "youtube_plan.json.invalid-20261014T000000"):
            (self.queue / name).write_text("{}", encoding="utf-8")
        self.assertEqual(["0001_x"], [item.run_dir.name for item in discover_queue_items(self.runner.config)])
        self.assertEqual([], YouTubeReplay(self.runner.config)._publish_states())


if __name__ == "__main__":
    unittest.main()
