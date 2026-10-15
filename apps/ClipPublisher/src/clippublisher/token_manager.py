from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from clippublisher.config import AppConfig

logger = logging.getLogger(__name__)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _token_request(url: str, *, params: dict[str, str]) -> requests.Response:
    try:
        return requests.get(url, params=params, timeout=(10, 60))
    except requests.RequestException:
        raise RuntimeError("Meta token request failed; saved credentials were not changed. Check network connectivity and Meta credentials.") from None


def _debug_token(config: AppConfig, access_token: str) -> dict[str, Any]:
    app_access_token = f"{config.facebook_app_id}|{config.facebook_app_secret}"
    response = _token_request(
        "https://graph.facebook.com/v20.0/debug_token",
        params={
            "input_token": access_token,
            "access_token": app_access_token,
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Token debug request failed: HTTP {response.status_code}.")
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Token debug response was missing data payload.")
    return data


def _exchange_for_long_lived(config: AppConfig, access_token: str) -> tuple[str, int]:
    response = _token_request(
        "https://graph.facebook.com/v20.0/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": config.facebook_app_id,
            "client_secret": config.facebook_app_secret,
            "fb_exchange_token": access_token,
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Long-lived token exchange failed: HTTP {response.status_code}.")
    payload = response.json()
    token = str(payload.get("access_token") or "").strip()
    expires_in = int(payload.get("expires_in") or 0)
    if not token:
        raise RuntimeError("Long-lived token exchange returned no access_token.")
    return token, expires_in


def _expiry_from_debug(data: dict[str, Any]) -> datetime | None:
    raw = data.get("expires_at")
    if isinstance(raw, int) and raw > 0:
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    return None


def _save_token_state(config: AppConfig, *, access_token: str, expires_at: datetime | None, source: str) -> None:
    payload: dict[str, Any] = {
        "access_token": access_token,
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at.isoformat()
    _write_json(config.token_state_file, payload)


def replace_access_token(config: AppConfig, access_token: str) -> str:
    """Validate and exchange an explicit replacement before touching saved credentials."""
    access_token = access_token.strip()
    if not access_token:
        raise ValueError("Replacement Instagram token must not be empty.")
    if not config.facebook_app_id or not config.facebook_app_secret:
        raise ValueError("FACEBOOK_APP_ID and FACEBOOK_APP_SECRET are required to replace the token.")
    debug = _debug_token(config, access_token)
    if not debug.get("is_valid"):
        raise ValueError("Replacement Instagram token is invalid.")
    if str(debug.get("app_id")) != config.facebook_app_id:
        raise ValueError("Replacement token belongs to a different Meta app.")
    refreshed_token, _ = _exchange_for_long_lived(config, access_token)
    refreshed_debug = _debug_token(config, refreshed_token)
    if not refreshed_debug.get("is_valid") or str(refreshed_debug.get("app_id")) != config.facebook_app_id:
        raise ValueError("Exchanged Instagram token failed validation.")
    expires_at = _expiry_from_debug(refreshed_debug)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise ValueError("Exchanged Instagram token is expired.")
    _save_token_state(config, access_token=refreshed_token, expires_at=expires_at, source="replacement")
    logger.info("Replaced saved Instagram token.")
    return refreshed_token


def read_access_token(config: AppConfig) -> str:
    """Current token without validation, refresh or any state write."""
    token = str(_read_json(config.token_state_file).get("access_token") or "").strip() or config.instagram_access_token
    if not token:
        raise ValueError("INSTAGRAM_ACCESS_TOKEN is required.")
    return token


def resolve_publish_access_token(config: AppConfig, *, force_refresh: bool = False) -> str:
    state = _read_json(config.token_state_file)
    access_token = str(state.get("access_token") or "").strip() or config.instagram_access_token
    if not access_token:
        raise ValueError("INSTAGRAM_ACCESS_TOKEN is required for publishing.")

    if not config.auto_refresh_token and not force_refresh:
        return access_token

    if not config.facebook_app_id or not config.facebook_app_secret:
        if force_refresh:
            raise ValueError("FACEBOOK_APP_ID and FACEBOOK_APP_SECRET are required to refresh the token.")
        logger.info("Skipping token auto-refresh: FACEBOOK_APP_ID and FACEBOOK_APP_SECRET are not configured.")
        return access_token

    debug = _debug_token(config, access_token)
    if not bool(debug.get("is_valid", False)):
        raise RuntimeError("Current Instagram access token is invalid. Re-run OAuth login and token exchange.")

    expires_at = _expiry_from_debug(debug)
    if expires_at is None and not force_refresh:
        _save_token_state(config, access_token=access_token, expires_at=None, source="debug_no_expiry")
        return access_token

    refresh_before = datetime.now(timezone.utc) + timedelta(days=max(1, config.refresh_buffer_days))
    if not force_refresh and expires_at > refresh_before:
        _save_token_state(config, access_token=access_token, expires_at=expires_at, source="debug_valid")
        return access_token

    refreshed_token, expires_in = _exchange_for_long_lived(config, access_token)
    refreshed_expiry = datetime.now(timezone.utc) + timedelta(seconds=max(0, expires_in))
    _save_token_state(
        config,
        access_token=refreshed_token,
        expires_at=refreshed_expiry if expires_in > 0 else None,
        source="refreshed",
    )
    logger.info("Refreshed Instagram access token; next expiry at %s.", refreshed_expiry.isoformat())
    return refreshed_token
