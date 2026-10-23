from __future__ import annotations

import os
import re
from pathlib import Path


def read_dotenv(dotenv_path: Path) -> dict[str, str]:
    """Key/value pairs of a .env file without touching the process environment."""
    values: dict[str, str] = {}
    if not dotenv_path.exists():
        return values
    for raw_line in dotenv_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_dotenv(dotenv_path: Path, *, override: bool = False) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed_key = key.strip()
        parsed_value = value.strip().strip('"').strip("'")
        account = os.environ.get("ACCOUNT_NAME", "")
        if account:
            parsed_value = parsed_value.replace("{ACCOUNT_NAME}", account)
        if override:
            os.environ[parsed_key] = parsed_value
        else:
            os.environ.setdefault(parsed_key, parsed_value)


def _find_repo_root(app_root: Path) -> Path:
    for candidate in [app_root, *app_root.parents]:
        if (candidate / "apps").exists() and (candidate / "README.md").exists():
            return candidate
    return app_root.parent


DEFAULT_PROFILE = "default"


def available_accounts(repo_root: Path) -> list[str]:
    return sorted(
        path.stem for path in (repo_root / "accounts").glob("*.env")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", path.stem)
    )


def active_profile() -> str:
    """Profile name chosen by the launcher (CLIPPER_PROFILE); `default` when unset."""
    return (os.getenv("CLIPPER_PROFILE") or DEFAULT_PROFILE).strip().lower()


def _load_profile_env(repo_root: Path) -> None:
    profile = active_profile()
    if not os.getenv("CLIPPER_PROFILE"):
        available_profiles = available_accounts(repo_root)
        if available_profiles:
            profile = available_profiles[0]
    os.environ["CLIPPER_PROFILE"] = profile
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", profile):
        raise ValueError(f"Invalid account selector: {profile}")
    profile_env = repo_root / "accounts" / f"{profile}.env"
    if not profile_env.exists():
        raise FileNotFoundError(f"Account '{profile}' has no config file at {profile_env}.")
    load_dotenv(profile_env, override=False)
    required = ("ACCOUNT_NAME", "CREATORS")
    missing = [key for key in required if not os.getenv(key, "").strip()]
    if missing:
        raise ValueError(f"Account '{profile}' ({profile_env}) is missing required settings: {', '.join(missing)}")
    from common.creators import configured_creators

    creators = configured_creators(repo_root)
    if not creators or not any(c.instagram_posts_per_day or c.youtube_posts_per_day for c in creators):
        raise ValueError(f"Account '{profile}' needs at least one creator with a positive daily quota.")
    account = os.environ["ACCOUNT_NAME"]
    base = f"data/state/{account}"
    defaults = {
        "QUEUE_DIR": f"{base}/queue",
        "STATE_FILE": f"{base}/state.json",
        "CANDIDATE_CACHE_FILE": f"{base}/candidate_cache_{{CREATOR_NAME}}.json",
        "TOKEN_STATE_FILE": f"{base}/publisher_token_state.json",
        "YOUTUBE_TOKEN_FILE": f"{base}/youtube_token.json",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    os.environ["INSTAGRAM_POSTS_PER_DAY"] = str(sum(c.instagram_posts_per_day for c in creators))
    os.environ["YOUTUBE_POSTS_PER_DAY"] = str(sum(c.youtube_posts_per_day for c in creators))
    # The first creator supplies defaults for standalone tools and pre-migration queue items.
    for key in ("STREAMER_NAME", "STREAMER_HASHTAG", "OVERLAY_IMAGE_PATH"):
        os.environ.setdefault(key, creators[0].settings[key])


def resolve_asset_path(raw: str, *bases: Path) -> Path:
    """Resolve a branding image: absolute path, relative to a base, or just a file name inside data/assets."""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    for base in bases:
        for candidate in (base / path, base / "data" / "assets" / path.name):
            if candidate.exists():
                return candidate
    return bases[0] / path


def is_test_mode() -> bool:
    return parse_bool(os.getenv("CLIPPER_TEST_MODE"), strict=False)


def _apply_test_mode(repo_root: Path) -> None:
    """In test mode every app works inside <QUEUE_DIR>/Test so nothing lands in the production queue."""
    if not is_test_mode():
        return
    test_dir = os.environ.get("CLIPPER_TEST_QUEUE_DIR")
    if not test_dir:
        base = Path(os.getenv("QUEUE_DIR", "data/queue").strip()).expanduser()
        if not base.is_absolute():
            base = repo_root / base
        test_dir = str(base / "Test")
        os.environ["CLIPPER_TEST_QUEUE_DIR"] = test_dir
    os.environ["QUEUE_DIR"] = test_dir


def load_app_env(app_root: Path) -> Path:
    app_root = app_root.resolve()
    repo_root = _find_repo_root(app_root)
    shared_env = repo_root / ".env"
    app_env = app_root / ".env"

    # Precedence: process env > account/creator config > app .env > root .env
    _load_profile_env(repo_root)
    load_dotenv(app_env, override=False)
    if shared_env != app_env:
        load_dotenv(shared_env, override=False)
    _apply_test_mode(repo_root)
    return repo_root


def looks_like_windows_path(value: str | None) -> bool:
    raw = (value or "").strip()
    return bool(re.match(r"^[A-Za-z]:\\", raw))


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_bool(value: str | None, *, default: bool = False, strict: bool = True) -> bool:
    normalized = (value or "").strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    if strict:
        raise ValueError(f"Invalid boolean value: {value}")
    return default
