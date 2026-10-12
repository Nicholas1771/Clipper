from __future__ import annotations

import os
import re
from pathlib import Path


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


def active_profile() -> str:
    """Profile name chosen by the launcher (CLIPPER_PROFILE); `default` when unset."""
    return (os.getenv("CLIPPER_PROFILE") or DEFAULT_PROFILE).strip().lower()


def _load_profile_env(repo_root: Path) -> None:
    profile = active_profile()
    if not os.getenv("CLIPPER_PROFILE"):
        available_profiles = sorted((repo_root / "profiles").glob("*.env"))
        if available_profiles:
            profile = available_profiles[0].stem.lower()
    os.environ["CLIPPER_PROFILE"] = profile
    profile_env = repo_root / "profiles" / f"{profile}.env"
    if not profile_env.exists():
        raise FileNotFoundError(f"Profile '{profile}' has no config file at {profile_env}.")
    load_dotenv(profile_env, override=False)
    local_profile_env = repo_root / "profiles" / f"{profile}.local.env"
    load_dotenv(local_profile_env, override=False)
    required = ("ACCOUNT_NAME", "STREAMER_NAME", "STREAMER_HASHTAG", "QUEUE_DIR", "OVERLAY_IMAGE_PATH")
    missing = [key for key in required if not os.getenv(key, "").strip()]
    if missing:
        raise ValueError(f"Profile '{profile}' ({profile_env}) is missing required settings: {', '.join(missing)}")


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
    os.environ["OUTPUT_DIR"] = test_dir


def load_app_env(app_root: Path) -> Path:
    app_root = app_root.resolve()
    repo_root = _find_repo_root(app_root)
    shared_env = repo_root / ".env"
    app_env = app_root / ".env"

    # Precedence: process env > profile-local secrets > profile config > app .env > root .env
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
