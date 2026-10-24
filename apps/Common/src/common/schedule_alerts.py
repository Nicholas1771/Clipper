from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from common.alerts import environment_tag, render_alert_html, send_email_alert
from common.env import _find_repo_root, available_accounts, is_test_mode, looks_like_windows_path
from common.platforms import INSTAGRAM, PLATFORM_LABELS, YOUTUBE
from common.reporting import ApifyUsage, apify_usage, posts_for_day
from common.gemini_usage import GeminiUsage, gemini_usage
from common.creators import configured_creators

logger = logging.getLogger(__name__)
DISPLAY_TZ = ZoneInfo("America/New_York")


def alert_timestamp(when: datetime) -> str:
    return when.astimezone(DISPLAY_TZ).strftime("%Y%m%d %H:%M:%S")


def schedule_rows(slots: list[ScheduleSlot], immediate: ScheduleSlot | None = None) -> list[str]:
    entries = [(slot, False) for slot in slots]
    if immediate is not None:
        entries.append((immediate, True))
    entries.sort(key=lambda entry: entry[0].time)
    return [
        f"{'Run Now' if is_now else 'Run ' + str(index)}: "
        f"{alert_timestamp(slot.time)} | {slot.label} | {slot.platforms}"
        for index, (slot, is_now) in enumerate(entries, 1)
    ] or ["No runs planned."]

LIGHTSAIL_ESTIMATE_START = date(2026, 10, 11)
LIGHTSAIL_ESTIMATE_AS_OF = date(2026, 10, 22)
LIGHTSAIL_DAILY_CAD = (
    Decimal("2.19") * Decimal("1.4021")
    / (LIGHTSAIL_ESTIMATE_AS_OF - LIGHTSAIL_ESTIMATE_START).days
)


@dataclass
class ProfileConfig:
    profile_name: str
    account_name: str
    queue_dir: Path
    instagram_posts_per_day: int
    youtube_posts_per_day: int
    instagram_user_id: str
    instagram_access_token: str
    token_state_file: Path | None
    youtube_client_id: str
    youtube_client_secret: str
    youtube_token_file: Path | None
    schedule_start_hour: int
    schedule_end_hour: int
    timezone_name: str
    youtube_selection_mode: str = "shared"


def _profile_plans_ready(profile: ProfileConfig, today: date) -> bool:
    """True once the profile has written today's Instagram plan and, when it replays to YouTube, today's YouTube plan."""
    files = [profile.queue_dir / "scheduler_plan.json"]
    if profile.youtube_selection_mode == "instagram_top" and profile.youtube_posts_per_day > 0:
        files.append(profile.queue_dir / "youtube_plan.json")
    for path in files:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("date") != today.isoformat():
                return False
        except (OSError, ValueError, AttributeError):
            return False
    return True

@dataclass
class ScheduleSlot:
    time: datetime
    account: str
    platforms: str
    is_remaining: bool = True
    is_overdue: bool = False
    creator: str = ""

    @property
    def label(self) -> str:
        return f"{self.account} | {self.creator}" if self.creator else self.account


def _parse_simple_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def discover_profile_configs(repo_root: Path) -> list[ProfileConfig]:
    profiles_dir = repo_root / "accounts"
    if not profiles_dir.exists():
        return []
    root_env = _parse_simple_env(repo_root / ".env")
    root_env.update(_parse_simple_env(repo_root / "apps" / "ClipPublisher" / ".env"))
    root_env.update(_parse_simple_env(repo_root / "apps" / "ClipScheduler" / ".env"))
    configs: list[ProfileConfig] = []

    for profile_name in available_accounts(repo_root):
        env_file = profiles_dir / f"{profile_name}.env"
        p_env = _parse_simple_env(env_file)
        merged: dict[str, str] = dict(root_env)
        merged.update(p_env)

        account = merged.get("ACCOUNT_NAME", profile_name).strip()
        queue_raw = merged.get("QUEUE_DIR", f"data/state/{account}/queue").replace("{ACCOUNT_NAME}", account)
        if os.name != "nt" and looks_like_windows_path(queue_raw):
            queue_raw = f"data/state/{account}/queue"
        queue_dir = Path(queue_raw).expanduser()
        if not queue_dir.is_absolute():
            queue_dir = repo_root / queue_dir

        token_state_raw = merged.get("TOKEN_STATE_FILE", f"data/state/{account}/publisher_token_state.json").replace("{ACCOUNT_NAME}", account)
        if os.name != "nt" and looks_like_windows_path(token_state_raw):
            token_state_raw = f"data/state/{account}/publisher_token_state.json"
        token_state_path = Path(token_state_raw).expanduser()
        if not token_state_path.is_absolute():
            token_state_path = repo_root / token_state_path

        yt_token_raw = merged.get("YOUTUBE_TOKEN_FILE", f"data/state/{account}/youtube_token.json").replace("{ACCOUNT_NAME}", account)
        if os.name != "nt" and looks_like_windows_path(yt_token_raw):
            yt_token_raw = f"data/state/{account}/youtube_token.json"
        yt_token_path = Path(yt_token_raw).expanduser()
        if not yt_token_path.is_absolute():
            yt_token_path = repo_root / yt_token_path

        creators = configured_creators(repo_root, merged)
        ig_posts = sum(creator.instagram_posts_per_day for creator in creators)
        yt_posts = sum(creator.youtube_posts_per_day for creator in creators)
        try:
            start_hour = int(merged.get("SCHEDULE_START_HOUR") or "8")
        except ValueError:
            start_hour = 8
        try:
            end_hour = int(merged.get("SCHEDULE_END_HOUR") or "23")
        except ValueError:
            end_hour = 23

        configs.append(
            ProfileConfig(
                profile_name=profile_name,
                account_name=account,
                queue_dir=queue_dir,
                instagram_posts_per_day=ig_posts,
                youtube_posts_per_day=yt_posts,
                instagram_user_id=merged.get("INSTAGRAM_USER_ID", "").strip(),
                instagram_access_token=merged.get("INSTAGRAM_ACCESS_TOKEN", "").strip(),
                token_state_file=token_state_path,
                youtube_client_id=merged.get("YOUTUBE_CLIENT_ID", "").strip(),
                youtube_client_secret=merged.get("YOUTUBE_CLIENT_SECRET", "").strip(),
                youtube_token_file=yt_token_path,
                schedule_start_hour=start_hour,
                schedule_end_hour=end_hour,
                timezone_name=merged.get("TIMEZONE_NAME", "America/New_York").strip(),
                youtube_selection_mode=(merged.get("YOUTUBE_SELECTION_MODE") or "shared").strip().lower(),
            )
        )
    return configs


def check_platform_status(profile: ProfileConfig) -> dict[str, str]:
    status: dict[str, str] = {}

    # Instagram check
    if profile.instagram_posts_per_day <= 0:
        status["Instagram"] = "Disabled (0 posts/day)"
    elif not profile.instagram_user_id:
        status["Instagram"] = "ERROR - INSTAGRAM_USER_ID is required for publishing"
    elif profile.token_state_file and profile.token_state_file.exists():
        try:
            payload = json.loads(profile.token_state_file.read_text(encoding="utf-8"))
            access_token = str(payload.get("access_token") or "").strip()
            expires_at_raw = payload.get("expires_at")
            if expires_at_raw:
                try:
                    expires_dt = datetime.fromisoformat(str(expires_at_raw))
                    if expires_dt.tzinfo is None:
                        expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                    if expires_dt <= datetime.now(timezone.utc):
                        status["Instagram"] = f"ERROR - Token expired at {expires_at_raw}"
                    else:
                        status["Instagram"] = "OK"
                except ValueError:
                    status["Instagram"] = "OK"
            elif access_token or profile.instagram_access_token:
                status["Instagram"] = "OK"
            else:
                status["Instagram"] = "ERROR - INSTAGRAM_ACCESS_TOKEN is required for publishing"
        except Exception as error:
            status["Instagram"] = f"ERROR - Failed to read token state: {error}"
    elif not profile.instagram_access_token:
        status["Instagram"] = "ERROR - INSTAGRAM_ACCESS_TOKEN is required for publishing"
    else:
        status["Instagram"] = "OK"

    # YouTube check
    if profile.youtube_posts_per_day <= 0:
        status["YouTube"] = "Disabled (0 posts/day)"
    elif not profile.youtube_client_id or not profile.youtube_client_secret:
        status["YouTube"] = "ERROR - YOUTUBE_CLIENT_ID or YOUTUBE_CLIENT_SECRET is missing"
    elif not profile.youtube_token_file or not profile.youtube_token_file.exists():
        status["YouTube"] = "ERROR - Refresh token file missing (run --youtube-auth)"
    else:
        try:
            payload = json.loads(profile.youtube_token_file.read_text(encoding="utf-8"))
            refresh_token = str(payload.get("refresh_token") or "").strip()
            if not refresh_token:
                status["YouTube"] = f"ERROR - No refresh token in {profile.youtube_token_file.name}"
            else:
                status["YouTube"] = "OK"
        except Exception as error:
            status["YouTube"] = f"ERROR - Failed to read YouTube token file: {error}"

    return status


def format_platforms_label(platforms: list[str]) -> str:
    cleaned = [PLATFORM_LABELS.get(p.lower(), p.capitalize()) for p in platforms if p]
    if not cleaned:
        return "Instagram"
    return ", ".join(cleaned)


def collect_schedule_slots(
    repo_root: Path,
    target_date: date,
    now: datetime,
    fallback_plan: object | None = None,
    fallback_config: object | None = None,
    immediate: datetime | None = None,
) -> tuple[list[ScheduleSlot], list[ScheduleSlot], ScheduleSlot | None]:
    profiles = discover_profile_configs(repo_root)
    all_slots: list[ScheduleSlot] = []
    immediate_slot: ScheduleSlot | None = None
    seen_accounts: set[str] = set()

    for profile in profiles:
        seen_accounts.add(profile.account_name)
        plan_file = profile.queue_dir / "scheduler_plan.json"
        if not plan_file.exists():
            continue
        try:
            data = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("date") or "") != target_date.isoformat():
            continue

        raw_slots = data.get("slots") or []
        raw_platforms = data.get("platforms") or []
        raw_creators = data.get("creator_names") or []
        next_index = data.get("next_index")
        next_index = next_index if isinstance(next_index, int) else 0

        for idx, slot_str in enumerate(raw_slots):
            try:
                slot_time = datetime.fromisoformat(str(slot_str))
                slot_time = slot_time.astimezone(ZoneInfo(profile.timezone_name))
            except (ValueError, Exception):
                continue
            plat_list = raw_platforms[idx] if idx < len(raw_platforms) and isinstance(raw_platforms[idx], list) else ["instagram"]
            plat_label = format_platforms_label(plat_list)
            is_remaining = (idx >= next_index) and (slot_time >= now or idx == next_index)
            is_overdue = (slot_time <= now) and is_remaining
            all_slots.append(
                ScheduleSlot(
                    time=slot_time,
                    account=profile.account_name,
                    platforms=plat_label,
                    is_remaining=is_remaining,
                    is_overdue=is_overdue,
                    creator=str(raw_creators[idx]) if idx < len(raw_creators) else "",
                )
            )

        youtube_file = profile.queue_dir / "youtube_plan.json"
        if youtube_file.exists():
            try:
                youtube_plan = json.loads(youtube_file.read_text(encoding="utf-8"))
                if youtube_plan.get("date") == target_date.isoformat():
                    for entry in youtube_plan.get("entries", []):
                        slot_time = datetime.fromisoformat(entry.get("scheduled_for") or entry["slot"])
                        slot_time = slot_time.astimezone(ZoneInfo(profile.timezone_name))
                        pending = entry.get("status") in {"pending", "planned"}
                        all_slots.append(ScheduleSlot(
                            time=slot_time,
                            account=profile.account_name,
                            platforms="YouTube",
                            is_remaining=pending,
                            is_overdue=pending and slot_time <= now,
                            creator=str(entry.get("creator_name") or ""),
                        ))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                logger.warning("Could not read YouTube schedule for account %s.", profile.account_name, exc_info=True)

    # If in standalone runner or test without discovered profile plans on disk
    if fallback_plan is not None and getattr(fallback_plan, "slots", None):
        fb_account = (
            getattr(fallback_config, "account_name", "")
            or os.getenv("ACCOUNT_NAME", "")
            or "Clipper"
        )
        if fb_account not in seen_accounts or not all_slots:
            fb_slots = getattr(fallback_plan, "slots", [])
            fb_platforms = getattr(fallback_plan, "platforms", [])
            fb_creators = getattr(fallback_plan, "creator_names", [])
            fb_next_index = getattr(fallback_plan, "next_index", 0)
            for idx, slot_time in enumerate(fb_slots):
                plat_list = fb_platforms[idx] if idx < len(fb_platforms) and isinstance(fb_platforms[idx], list) else ["instagram"]
                plat_label = format_platforms_label(plat_list)
                is_remaining = (idx >= fb_next_index) and (slot_time >= now or idx == fb_next_index)
                is_overdue = (slot_time <= now) and is_remaining
                all_slots.append(
                    ScheduleSlot(
                        time=slot_time,
                        account=fb_account,
                        platforms=plat_label,
                        is_remaining=is_remaining,
                        is_overdue=is_overdue,
                        creator=str(fb_creators[idx]) if idx < len(fb_creators) else "",
                    )
                )

    if immediate is not None:
        account_name = getattr(fallback_config, "account_name", "") or os.getenv("ACCOUNT_NAME", "") or "Clipper"
        immediate_slot = ScheduleSlot(
            time=immediate,
            account=account_name,
            platforms=format_platforms_label(getattr(fallback_config, "immediate_platforms", []))
            if getattr(fallback_config, "immediate_platforms", None)
            else "Instagram" if getattr(fallback_config, "youtube_selection_mode", "shared") == "instagram_top" else "Instagram, YouTube",
            is_remaining=True,
            is_overdue=False,
            creator=getattr(fallback_config, "immediate_creator_name", ""),
        )

    all_slots.sort(key=lambda s: s.time)
    remaining_slots = [s for s in all_slots if s.is_remaining]
    return all_slots, remaining_slots, immediate_slot


def build_started_alert_body(
    profiles_status: dict[str, dict[str, str]],
    remaining_slots: list[ScheduleSlot],
    today: date,
    tz_name: str,
    start_hour: int,
    end_hour: int,
    immediate_slot: ScheduleSlot | None = None,
) -> str:
    lines = [
        "Started",
        f"Date: {today.strftime('%Y%m%d')}",
        f"Window: {start_hour:02d}:00-{end_hour:02d}:00",
        f"Runs remaining: {len(remaining_slots)}",
        "",
        "Status",
    ]
    for account, platforms in profiles_status.items():
        for plat_name, plat_status in platforms.items():
            if not plat_status.lower().startswith("disabled"):
                lines.append(f"{account} {plat_name}: {plat_status}")
    lines.append("")
    lines.append("Remaining Schedule")
    lines.extend(schedule_rows(remaining_slots, immediate_slot))
    return "\n".join(lines) + "\n"


def build_schedule_alert_body(
    all_slots: list[ScheduleSlot],
    today: date,
    tz_name: str,
    start_hour: int,
    end_hour: int,
    immediate_slot: ScheduleSlot | None = None,
) -> str:
    lines = [
        "Schedule",
        f"Date: {today.strftime('%Y%m%d')}",
        f"Window: {start_hour:02d}:00-{end_hour:02d}:00",
        f"Runs planned: {len(all_slots) + int(immediate_slot is not None)}",
        "",
        "Planned Schedule",
    ]
    lines.extend(schedule_rows(all_slots, immediate_slot))
    return "\n".join(lines) + "\n"


def schedule_alert_html(body: str) -> str:
    sections = {
        "Started", "Schedule", "Daily Report", "Status", "Remaining Schedule",
        "Planned Schedule", "Post Status", "Usage", "Monthly Cost",
    }
    sections.update(line for line in body.splitlines() if line.startswith(("Account - ", "Usage - ")))
    return render_alert_html(body, section_titles=sections)


@contextlib.contextmanager
def file_lock(lock_path: Path, timeout_seconds: float = 15.0):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    acquired = False
    start = time.time()
    try:
        while time.time() - start < timeout_seconds:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError):
                time.sleep(0.1)
        if not acquired:
            logger.warning("Timed out waiting for file lock %s after %ss", lock_path, timeout_seconds)
            raise TimeoutError(f"Could not acquire lock on {lock_path}")
        yield f
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def _coordinator_paths(repo_root: Path) -> tuple[Path, Path]:
    state_dir = repo_root / "data"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "alert_coordinator.json", state_dir / "alert.lock"


def _wait_for_profile_plans(repo_root: Path, today: date, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    profiles = discover_profile_configs(repo_root)
    while time.time() < deadline:
        if all(_profile_plans_ready(p, today) for p in profiles):
            return
        time.sleep(1.0)


def _read_coordinator(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_coordinator(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


STARTUP_COOLDOWN_SECONDS = 180  # 3 minutes
ALERT_LOCK_TIMEOUT_SECONDS = 120


def _slots_fingerprint(slots: list[ScheduleSlot]) -> str:
    rows = sorted(f"{slot.time.isoformat()}|{slot.account}|{slot.platforms}" for slot in slots)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _alert_settings(repo_root: Path, fallback_config: object | None) -> tuple[list[ProfileConfig], str, int, int]:
    profiles = discover_profile_configs(repo_root)
    tz_name = (
        getattr(fallback_config, "timezone_name", "")
        or (profiles[0].timezone_name if profiles else "America/New_York")
    )
    start_hour = (
        getattr(fallback_config, "schedule_start_hour", 8)
        if fallback_config is not None
        else (profiles[0].schedule_start_hour if profiles else 8)
    )
    end_hour = (
        getattr(fallback_config, "schedule_end_hour", 23)
        if fallback_config is not None
        else (profiles[0].schedule_end_hour if profiles else 23)
    )
    return profiles, tz_name, start_hour, end_hour


def _send_coordinated_plan_alert(
    kind: str,
    repo_root: Path,
    now: datetime,
    fallback_plan: object | None,
    fallback_config: object | None,
    immediate: datetime | None,
    send_alert_fn: object | None,
) -> bool:
    if os.getenv("CLIPPER_TEST_MODE"):
        return False

    is_testing = "unittest" in sys.modules
    coordinator_file, lock_file = _coordinator_paths(repo_root)
    today = now.date()

    # Hold the lock through delivery so a concurrently starting profile waits, then sees the
    # recorded send. State is only recorded after a successful send so failures are retried.
    with file_lock(lock_file, timeout_seconds=ALERT_LOCK_TIMEOUT_SECONDS):
        if not is_testing:
            _wait_for_profile_plans(repo_root, today)

        all_slots, remaining_slots, immediate_slot = collect_schedule_slots(
            repo_root, today, now, fallback_plan=fallback_plan, fallback_config=fallback_config, immediate=immediate
        )
        fingerprint = _slots_fingerprint(all_slots)
        data = {} if is_testing else _read_coordinator(coordinator_file)
        if kind == "started":
            elapsed = time.time() - float(data.get("last_started_at", 0) or 0)
            if (
                data.get("last_started_fingerprint") == fingerprint
                and data.get("last_started_date") == today.isoformat()
                and 0 <= elapsed < STARTUP_COOLDOWN_SECONDS
            ):
                logger.info("Startup alert for this schedule already sent %s seconds ago; skipping duplicate.", int(elapsed))
                return False
        elif (
            data.get("last_schedule_date") == today.isoformat()
            and data.get("last_schedule_fingerprint") == fingerprint
        ):
            logger.info("Schedule alert for this schedule already sent for %s; skipping duplicate.", today.isoformat())
            return False

        profiles, tz_name, start_hour, end_hour = _alert_settings(repo_root, fallback_config)
        if kind == "started":
            status_map: dict[str, dict[str, str]] = {}
            if profiles:
                for p in profiles:
                    status_map[p.account_name] = check_platform_status(p)
            else:
                account_name = getattr(fallback_config, "account_name", "") or os.getenv("ACCOUNT_NAME", "") or "Clipper"
                status_map[account_name] = {"Instagram": "OK", "YouTube": "OK"}
            body = build_started_alert_body(
                profiles_status=status_map,
                remaining_slots=remaining_slots,
                today=today,
                tz_name=tz_name,
                start_hour=start_hour,
                end_hour=end_hour,
                immediate_slot=immediate_slot,
            )
            subject = f"Clip Start {today.strftime('%Y%m%d')} {environment_tag()}"
        else:
            body = build_schedule_alert_body(
                all_slots=all_slots,
                today=today,
                tz_name=tz_name,
                start_hour=start_hour,
                end_hour=end_hour,
                immediate_slot=immediate_slot,
            )
            subject = f"Clip Plan {today.strftime('%Y%m%d')} {environment_tag()}"

        sender = send_alert_fn or send_email_alert
        if not sender(
            app_name="ClipScheduler",
            subject=subject,
            body=body,
            html_body=schedule_alert_html(body),
            include_platform=False,
            include_account=False,
        ):
            logger.warning("%s alert email was not delivered; it will be retried on the next attempt.", kind.capitalize())
            return False
        if is_testing:
            return True

        data = _read_coordinator(coordinator_file)
        prefix = "last_started" if kind == "started" else "last_schedule"
        data[f"{prefix}_at"] = time.time()
        data[f"{prefix}_date"] = today.isoformat()
        data[f"{prefix}_fingerprint"] = fingerprint
        _write_coordinator(coordinator_file, data)
        return True


def send_consolidated_started_alert(
    repo_root: Path,
    now: datetime,
    fallback_plan: object | None = None,
    fallback_config: object | None = None,
    immediate: datetime | None = None,
    send_alert_fn: object | None = None,
) -> bool:
    return _send_coordinated_plan_alert(
        "started", repo_root, now, fallback_plan, fallback_config, immediate, send_alert_fn
    )


def send_consolidated_schedule_alert(
    repo_root: Path,
    now: datetime,
    fallback_plan: object | None = None,
    fallback_config: object | None = None,
    immediate: datetime | None = None,
    send_alert_fn: object | None = None,
) -> bool:
    return _send_coordinated_plan_alert(
        "schedule", repo_root, now, fallback_plan, fallback_config, immediate, send_alert_fn
    )


def build_daily_report_body(repo_root: Path, now: datetime, fallback_config: object) -> str:
    from common.cost_reports import apify_cost, cost_lines, monthly_cost_lines, usage_between, usage_lines

    today = now.date()
    tz_name = getattr(fallback_config, "timezone_name")
    profiles = discover_profile_configs(repo_root)
    accounts = {profile.queue_dir.resolve(): profile.account_name for profile in profiles}
    queue_dir = getattr(fallback_config, "queue_dir")
    accounts.setdefault(queue_dir.resolve(), os.getenv("ACCOUNT_NAME", "").strip() or "Clipper")
    slots, _, _ = collect_schedule_slots(repo_root, today, now)
    post_rows = []
    counts = []
    ran_accounts = []
    usage_blocks = []
    all_apify = []
    gemini_rows = []
    for queue, account in accounts.items():
        records = {(row["scheduled_for"], row["platform"]): row for row in posts_for_day(queue, today, tz_name)}
        # Plans from before tracking was installed must not be inferred successful from next_index.
        for slot in slots:
            if slot.account == account:
                for label in slot.platforms.split(", "):
                    platform = next((key for key, value in PLATFORM_LABELS.items() if value == label), label.lower())
                    records.setdefault((slot.time.isoformat(), platform), {
                        "scheduled_for": slot.time.isoformat(), "platform": platform,
                        "status": "unknown", "detail": "No recorded outcome",
                    })
        youtube_file = queue / "youtube_plan.json"
        if youtube_file.exists():
            try:
                plan = json.loads(youtube_file.read_text(encoding="utf-8"))
                if plan.get("date") == today.isoformat():
                    for entry in plan.get("entries", []):
                        when = entry.get("scheduled_for") or entry["slot"]
                        records[(when, YOUTUBE)] = {
                            "scheduled_for": when, "platform": YOUTUBE,
                            "status": entry.get("status", "unknown"),
                            "detail": entry.get("error") or entry.get("clip_id", ""),
                            "creator": entry.get("creator_name", ""),
                        }
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                logger.exception("Could not read YouTube post status for %s.", account)
                post_rows.append((now, f"{account} | YouTube status unavailable"))
        published_counts: dict[str, int] = {}
        if any(row["status"] not in {"planned", "pending", "unknown"} for row in records.values()):
            ran_accounts.append(account)
        for row in sorted(records.values(), key=lambda row: (datetime.fromisoformat(row["scheduled_for"]), row["platform"])):
            when = datetime.fromisoformat(row["scheduled_for"]).astimezone(ZoneInfo(tz_name))
            status = row["status"]
            if status in {"planned", "pending"}:
                status = "overdue / no recorded publish" if when <= now else "pending"
            if status in {"published", "success"}:
                published_counts[row["platform"]] = published_counts.get(row["platform"], 0) + 1
            detail = f" - {row['detail']}" if row["detail"] else ""
            creator = row.get("creator") or next(
                (slot.creator for slot in slots if slot.account == account and slot.time == when
                 and PLATFORM_LABELS.get(row["platform"], row["platform"]) in slot.platforms.split(", ")),
                "",
            )
            creator_text = f"{creator} | " if creator else ""
            post_rows.append((when, f"{alert_timestamp(when)} | {account} | "
                              f"{creator_text}{PLATFORM_LABELS.get(row['platform'], row['platform'])} | {status}{detail}"))
        platforms = {row["platform"] for row in records.values()}
        for platform in sorted(platforms):
            counts.append(f"{account} {PLATFORM_LABELS.get(platform, platform)} Posts: {published_counts.get(platform, 0)}")
        start = datetime.combine(today, datetime.min.time(), ZoneInfo(tz_name))
        apify_rows, ai_usage = usage_between(queue, start, now)
        all_apify.extend(apify_rows)
        gemini_rows.extend(ai_usage.rows)
        if (apify_rows or ai_usage.rows) and account not in ran_accounts:
            ran_accounts.append(account)
        creators = sorted({row["creator"] or "Unattributed" for row in [*apify_rows, *ai_usage.rows]})
        for creator in creators:
            apify_group = [row for row in apify_rows if (row["creator"] or "Unattributed") == creator]
            ai_group = GeminiUsage(tuple(row for row in ai_usage.rows if (row["creator"] or "Unattributed") == creator))
            usage_blocks.extend(["", f"Usage - {account} - {creator}", *usage_lines(apify_group, ai_group)])
    total_ai = GeminiUsage(tuple(gemini_rows))
    lines = [
        "Daily Report", f"Date: {today.strftime('%Y%m%d')}",
        f"Accounts: {', '.join(ran_accounts) or 'None'}", *counts,
        *cost_lines(apify_cost(all_apify), total_ai.cost_cad, LIGHTSAIL_DAILY_CAD),
        "", "Post Status",
        *[f"Run {index}: {text}" for index, (_, text) in enumerate(sorted(post_rows, key=lambda row: row[0]), 1)],
        "", "Usage", *usage_lines(all_apify, total_ai), *usage_blocks,
        "", "Monthly Cost",
        *monthly_cost_lines(repo_root, list(accounts), now, tz_name, LIGHTSAIL_DAILY_CAD),
    ]
    return "\n".join(lines) + "\n"


def send_consolidated_daily_report(
    repo_root: Path,
    now: datetime,
    fallback_config: object,
    send_alert_fn=None,
) -> bool:
    if is_test_mode():
        return False
    coordinator_file, lock_file = _coordinator_paths(repo_root)
    today = now.date().isoformat()
    # Keep the lock through delivery: failed sends can retry without duplicate sends from other profiles.
    with file_lock(lock_file, timeout_seconds=60):
        data = _read_coordinator(coordinator_file)
        if data.get("last_daily_report_date") == today:
            return True
        body = build_daily_report_body(repo_root, now, fallback_config)
        sender = send_alert_fn or send_email_alert
        if not sender(
            app_name="ClipScheduler",
            subject=f"Clip Report {now.strftime('%Y%m%d')} {environment_tag()}",
            body=body,
            html_body=schedule_alert_html(body),
            include_platform=False,
            include_account=False,
        ):
            return False
        data["last_daily_report_date"] = today
        _write_coordinator(coordinator_file, data)
        return True
