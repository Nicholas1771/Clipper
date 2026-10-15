from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from common.alerts import send_email_alert
from common.env import _find_repo_root, looks_like_windows_path
from common.platforms import INSTAGRAM, PLATFORM_LABELS, YOUTUBE

logger = logging.getLogger(__name__)


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
    profiles_dir = repo_root / "profiles"
    if not profiles_dir.exists():
        return []
    root_env = _parse_simple_env(repo_root / ".env")
    configs: list[ProfileConfig] = []

    for env_file in sorted(profiles_dir.glob("*.env")):
        if env_file.name.endswith(".local.env") or env_file.name.endswith(".example"):
            continue
        profile_name = env_file.stem.lower()
        p_env = _parse_simple_env(env_file)
        local_file = profiles_dir / f"{profile_name}.local.env"
        if local_file.exists():
            p_env.update(_parse_simple_env(local_file))

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

        try:
            ig_posts = int(merged.get("INSTAGRAM_POSTS_PER_DAY") or merged.get("POSTS_PER_DAY") or "5")
        except ValueError:
            ig_posts = 5
        try:
            yt_posts = int(merged.get("YOUTUBE_POSTS_PER_DAY") or "0")
        except ValueError:
            yt_posts = 0
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
                    )
                )

    if immediate is not None:
        account_name = getattr(fallback_config, "account_name", "") or os.getenv("ACCOUNT_NAME", "") or "Clipper"
        immediate_slot = ScheduleSlot(
            time=immediate,
            account=account_name,
            platforms="Instagram" if getattr(fallback_config, "youtube_selection_mode", "shared") == "instagram_top" else "Instagram, YouTube",
            is_remaining=True,
            is_overdue=False,
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
        f"Clipper Started for {today.strftime('%Y%m%d')} ({tz_name}).",
        f"Window: {start_hour:02d}:00-{end_hour:02d}:00, posts remaining: {len(remaining_slots)}",
        "",
        "Platform Status:",
    ]
    for account, platforms in profiles_status.items():
        lines.append(f"* {account}:")
        for plat_name, plat_status in platforms.items():
            lines.append(f"  - {plat_name}: {plat_status}")
    lines.append("")
    lines.append("Remaining Schedule:")
    if immediate_slot is not None:
        lines.append(f"  • Immediate run (--now): {immediate_slot.time.strftime('%Y-%m-%d %H:%M:%S %Z')} | {immediate_slot.account} | {immediate_slot.platforms}")
    if remaining_slots:
        for idx, slot in enumerate(remaining_slots, 1):
            overdue_str = " (overdue, runs on startup)" if slot.is_overdue else ""
            lines.append(f"  {idx}. {slot.time.strftime('%Y-%m-%d %H:%M:%S %Z')} | {slot.account} | {slot.platforms}{overdue_str}")
    else:
        lines.append("  No runs left in today's window.")
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
        f"Clipper New schedule for {today.strftime('%Y%m%d')} ({tz_name}).",
        f"Window: {start_hour:02d}:00-{end_hour:02d}:00, posts planned: {len(all_slots)}",
        "",
        "Planned Schedule:",
    ]
    if immediate_slot is not None:
        lines.append(f"  • Immediate run (--now): {immediate_slot.time.strftime('%Y-%m-%d %H:%M:%S %Z')} | {immediate_slot.account} | {immediate_slot.platforms}")
    if all_slots:
        for idx, slot in enumerate(all_slots, 1):
            lines.append(f"  {idx}. {slot.time.strftime('%Y-%m-%d %H:%M:%S %Z')} | {slot.account} | {slot.platforms}")
    else:
        lines.append("  No runs left in today's window.")
    return "\n".join(lines) + "\n"


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


def send_consolidated_started_alert(
    repo_root: Path,
    now: datetime,
    fallback_plan: object | None = None,
    fallback_config: object | None = None,
    immediate: datetime | None = None,
    send_alert_fn: object | None = None,
) -> bool:
    if os.getenv("CLIPPER_TEST_MODE"):
        return False

    is_testing = "unittest" in sys.modules
    coordinator_file, lock_file = _coordinator_paths(repo_root)

    # 1. Under cross-process lock: check and atomically claim
    with file_lock(lock_file):
        data = _read_coordinator(coordinator_file)
        last_started = float(data.get("last_started_at", 0))
        if not is_testing and (time.time() - last_started < STARTUP_COOLDOWN_SECONDS):
            logger.info(
                "Startup alert already claimed/sent %s seconds ago; skipping duplicate.",
                int(time.time() - last_started),
            )
            return False

        # Claim immediately under lock so concurrent service sees it already claimed
        data["last_started_at"] = time.time()
        data["last_started_date"] = now.date().isoformat()
        _write_coordinator(coordinator_file, data)

    # 2. Wait up to 30 seconds for every profile to write today's plans (including YouTube replay plans) to disk
    if not is_testing:
        _wait_for_profile_plans(repo_root, now.date())

    # 3. Gather status for all profiles
    profiles = discover_profile_configs(repo_root)
    status_map: dict[str, dict[str, str]] = {}
    if profiles:
        for p in profiles:
            status_map[p.account_name] = check_platform_status(p)
    else:
        account_name = getattr(fallback_config, "account_name", "") or os.getenv("ACCOUNT_NAME", "") or "Clipper"
        status_map[account_name] = {"Instagram": "OK", "YouTube": "OK"}

    today = now.date()
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

    all_slots, remaining_slots, immediate_slot = collect_schedule_slots(
        repo_root, today, now, fallback_plan=fallback_plan, fallback_config=fallback_config, immediate=immediate
    )

    body = build_started_alert_body(
        profiles_status=status_map,
        remaining_slots=remaining_slots,
        today=today,
        tz_name=tz_name,
        start_hour=start_hour,
        end_hour=end_hour,
        immediate_slot=immediate_slot,
    )
    subject = f"Started - {today.strftime('%Y%m%d')}"

    sender = send_alert_fn or send_email_alert
    return sender(
        app_name="ClipScheduler",
        subject=subject,
        body=body,
        include_platform=False,
        include_account=False,
    )


def send_consolidated_schedule_alert(
    repo_root: Path,
    now: datetime,
    fallback_plan: object | None = None,
    fallback_config: object | None = None,
    immediate: datetime | None = None,
    send_alert_fn: object | None = None,
) -> bool:
    if os.getenv("CLIPPER_TEST_MODE"):
        return False

    is_testing = "unittest" in sys.modules
    coordinator_file, lock_file = _coordinator_paths(repo_root)
    today = now.date()

    # 1. Under cross-process lock: check and atomically claim for today
    with file_lock(lock_file):
        data = _read_coordinator(coordinator_file)
        if not is_testing and data.get("last_schedule_date") == today.isoformat():
            logger.info("Schedule alert already claimed/sent for %s; skipping duplicate.", today.isoformat())
            return False

        # Claim immediately under lock so concurrent service rolls over and skips
        data["last_schedule_date"] = today.isoformat()
        data["last_schedule_at"] = time.time()
        _write_coordinator(coordinator_file, data)

    # 2. Wait up to 30 seconds for every profile to write today's plans (including YouTube replay plans) to disk
    if not is_testing:
        _wait_for_profile_plans(repo_root, today)

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

    all_slots, remaining_slots, immediate_slot = collect_schedule_slots(
        repo_root, today, now, fallback_plan=fallback_plan, fallback_config=fallback_config, immediate=immediate
    )

    body = build_schedule_alert_body(
        all_slots=all_slots,
        today=today,
        tz_name=tz_name,
        start_hour=start_hour,
        end_hour=end_hour,
        immediate_slot=immediate_slot,
    )
    subject = f"Schedule - {today.strftime('%Y%m%d')}"

    sender = send_alert_fn or send_email_alert
    return sender(
        app_name="ClipScheduler",
        subject=subject,
        body=body,
        include_platform=False,
        include_account=False,
    )
