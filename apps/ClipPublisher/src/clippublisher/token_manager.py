from __future__ import annotations

import json
import logging
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
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _debug_token(config: AppConfig, access_token: str) -> dict[str, Any]:
    app_access_token = f"{config.facebook_app_id}|{config.facebook_app_secret}"
    response = requests.get(
        "https://graph.facebook.com/v20.0/debug_token",
        params={
            "input_token": access_token,
            "access_token": app_access_token,
        },
        timeout=(10, 60),
    )
    if response.status_code >= 400:
        body = response.text
        try:
            body = json.dumps(response.json(), indent=2)
        except ValueError:
            pass
        raise RuntimeError(f"Token debug request failed: HTTP {response.status_code}. Response: {body}")
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Token debug response was missing data payload: {payload}")
    return data


def _exchange_for_long_lived(config: AppConfig, access_token: str) -> tuple[str, int]:
    response = requests.get(
        "https://graph.facebook.com/v20.0/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": config.facebook_app_id,
            "client_secret": config.facebook_app_secret,
            "fb_exchange_token": access_token,
        },
        timeout=(10, 60),
    )
    if response.status_code >= 400:
        body = response.text
        try:
            body = json.dumps(response.json(), indent=2)
        except ValueError:
            pass
        raise RuntimeError(f"Long-lived token exchange failed: HTTP {response.status_code}. Response: {body}")
    payload = response.json()
    token = str(payload.get("access_token") or "").strip()
    expires_in = int(payload.get("expires_in") or 0)
    if not token:
        raise RuntimeError(f"Long-lived token exchange returned no access_token: {payload}")
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


def resolve_publish_access_token(config: AppConfig, *, force_refresh: bool = False) -> str:
    state = _read_json(config.token_state_file)
    access_token = str(state.get("access_token") or "").strip() or config.instagram_access_token
    if not access_token:
        raise ValueError("INSTAGRAM_ACCESS_TOKEN is required for publishing.")

    if not config.auto_refresh_token:
        return access_token

    if not config.facebook_app_id or not config.facebook_app_secret:
        logger.info("Skipping token auto-refresh: FACEBOOK_APP_ID and FACEBOOK_APP_SECRET are not configured.")
        return access_token

    debug = _debug_token(config, access_token)
    if not bool(debug.get("is_valid", False)):
        raise RuntimeError("Current Instagram access token is invalid. Re-run OAuth login and token exchange.")

    expires_at = _expiry_from_debug(debug)
    if expires_at is None:
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
