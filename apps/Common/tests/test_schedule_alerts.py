from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from common.alerts import format_alert_subject
from common.schedule_alerts import (
    ProfileConfig,
    ScheduleSlot,
    _profile_plans_ready,
    alert_timestamp,
    build_schedule_alert_body,
    build_started_alert_body,
    check_platform_status,
    collect_schedule_slots,
    discover_profile_configs,
    send_consolidated_schedule_alert,
    send_consolidated_started_alert,
    schedule_alert_html,
)

TZ = ZoneInfo("America/New_York")


class ScheduleAlertFormattingTests(unittest.TestCase):
    def test_alert_timestamp_uses_dst_aware_eastern_time(self):
        summer = datetime(2026, 10, 24, 1, 22, tzinfo=timezone.utc)
        winter = datetime(2026, 1, 24, 2, 22, tzinfo=timezone.utc)

        self.assertEqual("20261023 21:22:00", alert_timestamp(summer))
        self.assertEqual("20260123 21:22:00", alert_timestamp(winter))

    def test_creator_names_appear_in_schedule_and_started_emails(self):
        slot = ScheduleSlot(
            time=datetime(2026, 10, 19, 18, tzinfo=TZ), account="ExampleAccount",
            platforms="Instagram", creator="Nevaeh",
        )
        schedule = build_schedule_alert_body([slot], slot.time.date(), "America/New_York", 8, 23)
        started = build_started_alert_body({}, [slot], slot.time.date(), "America/New_York", 8, 23)
        for body in (schedule, started):
            self.assertIn("ExampleAccount | Nevaeh | Instagram", body)

    def test_schedule_includes_selected_youtube_replay_not_empty_opportunities(self):
        with TemporaryDirectory() as tmp:
            queue = Path(tmp)
            profile = mock.Mock(
                queue_dir=queue, account_name="account", timezone_name="America/New_York",
            )
            (queue / "scheduler_plan.json").write_text(json.dumps({
                "date": "2026-10-14",
                "slots": ["2026-10-14T18:00:00-04:00"],
                "platforms": [["instagram"]],
                "next_index": 0,
                "youtube_slots": ["2026-10-14T11:00:00-04:00"],
            }))
            youtube_file = queue / "youtube_plan.json"
            youtube_file.write_text(json.dumps({
                "date": "2026-10-14",
                "entries": [{
                    "scheduled_for": "2026-10-14T11:00:00-04:00", "status": "pending",
                }],
            }))
            now = datetime(2026, 10, 14, 0, tzinfo=TZ)
            with mock.patch("common.schedule_alerts.discover_profile_configs", return_value=[profile]):
                all_slots, remaining, _ = collect_schedule_slots(queue, now.date(), now)
                self.assertEqual(["YouTube", "Instagram"], [slot.platforms for slot in all_slots])
                self.assertEqual(2, len(remaining))
                youtube_file.write_text(json.dumps({"date": "2026-10-14", "entries": []}))
                all_slots, _, _ = collect_schedule_slots(queue, now.date(), now)
                self.assertEqual(["Instagram"], [slot.platforms for slot in all_slots])

    def test_alert_waits_for_other_profiles_youtube_replay_plan(self):
        with TemporaryDirectory() as tmp:
            queue = Path(tmp)
            profile = mock.Mock(queue_dir=queue, youtube_selection_mode="instagram_top", youtube_posts_per_day=1)
            today = date(2026, 10, 15)
            (queue / "scheduler_plan.json").write_text(json.dumps({"date": "2026-10-15"}))
            self.assertFalse(_profile_plans_ready(profile, today))
            (queue / "youtube_plan.json").write_text(json.dumps({"date": "2026-10-14"}))
            self.assertFalse(_profile_plans_ready(profile, today))
            (queue / "youtube_plan.json").write_text(json.dumps({"date": "2026-10-15"}))
            self.assertTrue(_profile_plans_ready(profile, today))
            shared = mock.Mock(queue_dir=queue, youtube_selection_mode="shared", youtube_posts_per_day=1)
            (queue / "youtube_plan.json").unlink()
            self.assertTrue(_profile_plans_ready(shared, today))

    def test_subject_omits_platform_and_account_for_schedule_and_started(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] Schedule - 20261012",
                format_alert_subject(subject="Schedule - 20261012"),
            )
            self.assertEqual(
                "[PROD] Started - 20261012",
                format_alert_subject(subject="Started - 20261012"),
            )

    def test_check_platform_status_ok(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            token_state = tmp_path / "publisher_token_state.json"
            future_exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            token_state.write_text(json.dumps({"access_token": "tok123", "expires_at": future_exp}))

            yt_token = tmp_path / "youtube_token.json"
            yt_token.write_text(json.dumps({"refresh_token": "ref123"}))

            profile = ProfileConfig(
                profile_name="n3on",
                account_name="CarootClips",
                queue_dir=tmp_path / "queue",
                instagram_posts_per_day=5,
                youtube_posts_per_day=1,
                instagram_user_id="17841467426737362",
                instagram_access_token="tok123",
                token_state_file=token_state,
                youtube_client_id="yt_id",
                youtube_client_secret="yt_sec",
                youtube_token_file=yt_token,
                schedule_start_hour=8,
                schedule_end_hour=23,
                timezone_name="America/New_York",
            )

            status = check_platform_status(profile)
            self.assertEqual("OK", status["Instagram"])
            self.assertEqual("OK", status["YouTube"])

    def test_check_platform_status_catches_missing_instagram_user_id(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile = ProfileConfig(
                profile_name="navaeh",
                account_name="VrlClipMoments",
                queue_dir=tmp_path / "queue",
                instagram_posts_per_day=5,
                youtube_posts_per_day=1,
                instagram_user_id="",  # missing!
                instagram_access_token="tok123",
                token_state_file=None,
                youtube_client_id="yt_id",
                youtube_client_secret="yt_sec",
                youtube_token_file=None,
                schedule_start_hour=8,
                schedule_end_hour=23,
                timezone_name="America/New_York",
            )
            status = check_platform_status(profile)
            self.assertIn("ERROR - INSTAGRAM_USER_ID is required for publishing", status["Instagram"])
            self.assertIn("ERROR", status["YouTube"])

    def test_check_platform_status_catches_expired_token(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            token_state = tmp_path / "publisher_token_state.json"
            past_exp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            token_state.write_text(json.dumps({"access_token": "tok123", "expires_at": past_exp}))

            profile = ProfileConfig(
                profile_name="n3on",
                account_name="CarootClips",
                queue_dir=tmp_path / "queue",
                instagram_posts_per_day=5,
                youtube_posts_per_day=0,
                instagram_user_id="12345",
                instagram_access_token="tok123",
                token_state_file=token_state,
                youtube_client_id="",
                youtube_client_secret="",
                youtube_token_file=None,
                schedule_start_hour=8,
                schedule_end_hour=23,
                timezone_name="America/New_York",
            )
            status = check_platform_status(profile)
            self.assertIn("ERROR - Token expired", status["Instagram"])
            self.assertEqual("Disabled (0 posts/day)", status["YouTube"])

    def test_build_started_alert_body_formatting(self):
        status_map = {
            "CarootClips": {"Instagram": "OK", "YouTube": "OK"},
            "VrlClipMoments": {"Instagram": "ERROR - INSTAGRAM_USER_ID is required for publishing", "YouTube": "OK"},
        }
        remaining = [
            ScheduleSlot(
                time=datetime(2026, 10, 12, 20, 58, 27, tzinfo=TZ),
                account="CarootClips",
                platforms="Instagram",
                is_remaining=True,
            ),
            ScheduleSlot(
                time=datetime(2026, 10, 12, 21, 41, 23, tzinfo=TZ),
                account="VrlClipMoments",
                platforms="Instagram, YouTube",
                is_remaining=True,
            ),
        ]
        body = build_started_alert_body(
            profiles_status=status_map,
            remaining_slots=remaining,
            today=date(2026, 10, 12),
            tz_name="America/New_York",
            start_hour=8,
            end_hour=23,
        )
        self.assertTrue(body.startswith("Started\nDate: 20261012\n"))
        self.assertIn("Runs remaining: 2", body)
        self.assertIn("Status\n", body)
        self.assertIn("CarootClips Instagram: OK", body)
        self.assertIn("Instagram: OK", body)
        self.assertIn("YouTube: OK", body)
        self.assertIn("VrlClipMoments Instagram:", body)
        self.assertIn("Instagram: ERROR - INSTAGRAM_USER_ID is required for publishing", body)
        self.assertIn("Remaining Schedule\n", body)
        self.assertIn("Run 1: 20261012 20:58:27 | CarootClips | Instagram", body)
        self.assertIn("Run 2: 20261012 21:41:23 | VrlClipMoments | Instagram, YouTube", body)

    def test_build_schedule_alert_body_ordering(self):
        slots = [
            ScheduleSlot(
                time=datetime(2026, 10, 12, 10, 8, 6, tzinfo=TZ),
                account="CarootClips",
                platforms="Instagram",
            ),
            ScheduleSlot(
                time=datetime(2026, 10, 12, 11, 9, 15, tzinfo=TZ),
                account="VrlClipMoments",
                platforms="Instagram",
            ),
            ScheduleSlot(
                time=datetime(2026, 10, 12, 16, 52, 31, tzinfo=TZ),
                account="CarootClips",
                platforms="Instagram, YouTube",
            ),
        ]
        body = build_schedule_alert_body(
            all_slots=slots,
            today=date(2026, 10, 12),
            tz_name="America/New_York",
            start_hour=8,
            end_hour=23,
        )
        self.assertTrue(body.startswith("Schedule\nDate: 20261012\n"))
        self.assertIn("Runs planned: 3", body)
        self.assertIn("Planned Schedule\n", body)
        self.assertIn("Run 1: 20261012 10:08:06 | CarootClips | Instagram", body)
        self.assertIn("Run 2: 20261012 11:09:15 | VrlClipMoments | Instagram", body)
        self.assertIn("Run 3: 20261012 16:52:31 | CarootClips | Instagram, YouTube", body)

    def test_html_sections_escape_account_and_status_and_keep_schedule_times(self):
        slot = ScheduleSlot(datetime(2026, 10, 12, 18, tzinfo=timezone.utc), "<Account>", "Instagram", is_overdue=True)
        body = build_started_alert_body(
            {"<Account>": {"Instagram": "ERROR - <expired> & unavailable"}},
            [slot], date(2026, 10, 12), "America/New_York", 8, 23, immediate_slot=slot,
        )
        self.assertIn("20261012 14:00:00", body)
        self.assertNotIn("overdue", body)
        self.assertIn("Run Now:", body)
        self.assertNotIn("Immediate run", body)
        html = schedule_alert_html(body)
        self.assertIn("background:#eef2f6", html)
        self.assertIn("&lt;Account&gt; Instagram", html)
        self.assertIn("ERROR - &lt;expired&gt; &amp; unavailable", html)
        self.assertNotIn("<Account>", html)

    def test_all_plan_alerts_send_html_and_plain_text(self):
        for alert in (send_consolidated_started_alert, send_consolidated_schedule_alert):
            with self.subTest(alert=alert.__name__), TemporaryDirectory() as tmp:
                sender = mock.Mock(return_value=True)
                self.assertTrue(alert(Path(tmp), datetime(2026, 10, 12, 8, tzinfo=TZ), send_alert_fn=sender))
                call = sender.call_args.kwargs
                self.assertEqual(schedule_alert_html(call["body"]), call["html_body"])
                self.assertFalse(call["include_account"])
                self.assertFalse(call["include_platform"])

    def test_schedule_alert_deduplication_for_same_date(self):
        with TemporaryDirectory() as tmp:
            tmp_repo = Path(tmp)
            sent_count = 0

            def fake_send(*args, **kwargs):
                nonlocal sent_count
                sent_count += 1
                return True

            now = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)
            with mock.patch("common.schedule_alerts.sys.modules", {}):
                first = send_consolidated_schedule_alert(
                    tmp_repo, now, send_alert_fn=fake_send
                )
                second = send_consolidated_schedule_alert(
                    tmp_repo, now, send_alert_fn=fake_send
                )
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(1, sent_count)

    def test_started_alert_cooldown_deduplication(self):
        with TemporaryDirectory() as tmp:
            tmp_repo = Path(tmp)
            sent_count = 0

            def fake_send(*args, **kwargs):
                nonlocal sent_count
                sent_count += 1
                return True

            now = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)
            with mock.patch("common.schedule_alerts.sys.modules", {}):
                first = send_consolidated_started_alert(
                    tmp_repo, now, send_alert_fn=fake_send
                )
                second = send_consolidated_started_alert(
                    tmp_repo, now, send_alert_fn=fake_send
                )
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(1, sent_count)

    def _slot(self, hour):
        return [ScheduleSlot(time=datetime(2026, 10, 12, hour, tzinfo=TZ), account="A", platforms="Instagram")]

    def test_failed_alert_send_is_retried(self):
        for alert in (send_consolidated_started_alert, send_consolidated_schedule_alert):
            with self.subTest(alert=alert.__name__), TemporaryDirectory() as tmp:
                sender = mock.Mock(side_effect=[False, True, True])
                now = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)
                with mock.patch("common.schedule_alerts.sys", mock.Mock(modules={})):
                    self.assertFalse(alert(Path(tmp), now, send_alert_fn=sender))
                    self.assertTrue(alert(Path(tmp), now, send_alert_fn=sender))
                    self.assertFalse(alert(Path(tmp), now, send_alert_fn=sender))
                self.assertEqual(2, sender.call_count)

    def test_changed_schedule_sends_again_same_day(self):
        for alert in (send_consolidated_started_alert, send_consolidated_schedule_alert):
            with self.subTest(alert=alert.__name__), TemporaryDirectory() as tmp:
                sender = mock.Mock(return_value=True)
                now = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)
                slots = [(self._slot(10), [], None), (self._slot(10), [], None), (self._slot(12), [], None)]
                with mock.patch("common.schedule_alerts.sys", mock.Mock(modules={})), \
                     mock.patch("common.schedule_alerts.collect_schedule_slots", side_effect=slots):
                    self.assertTrue(alert(Path(tmp), now, send_alert_fn=sender))
                    self.assertFalse(alert(Path(tmp), now, send_alert_fn=sender))
                    self.assertTrue(alert(Path(tmp), now, send_alert_fn=sender))
                self.assertEqual(2, sender.call_count)

    def test_startup_alert_sends_again_after_cooldown(self):
        with TemporaryDirectory() as tmp:
            sender = mock.Mock(return_value=True)
            now = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)
            with mock.patch("common.schedule_alerts.sys", mock.Mock(modules={})), \
                 mock.patch("common.schedule_alerts.time.time", side_effect=lambda: clock[0]):
                clock = [1000.0]
                self.assertTrue(send_consolidated_started_alert(Path(tmp), now, send_alert_fn=sender))
                clock[0] += 60
                self.assertFalse(send_consolidated_started_alert(Path(tmp), now, send_alert_fn=sender))
                clock[0] += 200
                self.assertTrue(send_consolidated_started_alert(Path(tmp), now, send_alert_fn=sender))
            self.assertEqual(2, sender.call_count)


if __name__ == "__main__":
    unittest.main()
