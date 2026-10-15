from __future__ import annotations

import logging
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

from common.env import parse_bool


logger = logging.getLogger(__name__)


def environment_tag() -> str:
    env = (os.getenv("CLIPPER_ENV") or os.getenv("ENVIRONMENT") or "").strip().upper()
    if env in {"PROD", "PRODUCTION"}:
        return "PROD"
    if env in {"LOCAL", "DEV", "DEVELOPMENT", "TEST"}:
        return "LOCAL"
    # Auto-detect: on the EC2 production instance
    if os.name != "nt" and (Path("/home/ubuntu/Clipper").exists() or os.getenv("USER") == "ubuntu"):
        return "PROD"
    return "LOCAL"


def platform_tag(platform: str) -> str:
    norm = platform.strip().lower() if platform else ""
    if norm in {"instagram", "ig"}:
        return "IG"
    if norm in {"youtube", "yt"}:
        return "YT"
    return "IG/YT"


def format_alert_subject(
    *,
    subject: str,
    platform: str = "",
    include_platform: bool | None = None,
    include_account: bool | None = None,
) -> str:
    env = environment_tag()
    plat = platform_tag(platform)
    account = os.getenv("ACCOUNT_NAME", "").strip() or "Clipper"

    rest = subject.strip()
    patterns = [
        r"^\[(PROD|LOCAL)\]\s*",
        r"^\[(IG|YT|IG/YT)\]\s*",
        rf"^\[{re.escape(account)}\]\s*",
        rf"^{re.escape(account)}\s*",
        r"^\[?Clipper\]?\s*",
        r"^\[?Instagram\]?\s*",
        r"^\[?YouTube\]?\s*",
    ]
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            cleaned = re.sub(pattern, "", rest, flags=re.IGNORECASE).strip()
            if cleaned != rest:
                rest = cleaned
                changed = True

    is_schedule_or_start = bool(re.match(r"^(Schedule|Started)\b", rest, re.IGNORECASE))
    if include_platform is None:
        include_platform = not is_schedule_or_start
    if include_account is None:
        include_account = not is_schedule_or_start

    parts = [f"[{env}]"]
    if include_platform:
        parts.append(f"[{plat}]")
    if include_account:
        parts.append(f"[{account}]")
    parts.append(rest)
    return " ".join(parts)


def send_email_alert(
    *,
    app_name: str,
    subject: str,
    body: str,
    platform: str = "",
    include_platform: bool | None = None,
    include_account: bool | None = None,
) -> bool:
    enabled = parse_bool(os.getenv("ALERT_EMAIL_ENABLED"), default=True, strict=False)
    if not enabled:
        return False
    smtp_host = os.getenv("ALERT_EMAIL_SMTP_HOST", "").strip()
    if not smtp_host:
        logger.warning("Email alert skipped because ALERT_EMAIL_SMTP_HOST is not configured.")
        return False
    smtp_port = int(os.getenv("ALERT_EMAIL_SMTP_PORT", "587"))
    use_tls = parse_bool(os.getenv("ALERT_EMAIL_USE_TLS"), default=True, strict=False)
    username = os.getenv("ALERT_EMAIL_USERNAME", "").strip()
    password = os.getenv("ALERT_EMAIL_PASSWORD", "").strip()
    from_address = os.getenv("ALERT_EMAIL_FROM", "").strip() or username
    to_address = os.getenv("ALERT_EMAIL_TO", "").strip()
    if not to_address:
        logger.warning("Email alert skipped because ALERT_EMAIL_TO is not configured.")
        return False
    timeout_seconds = int(os.getenv("ALERT_EMAIL_TIMEOUT_SECONDS", "20"))
    if not from_address:
        logger.warning("Email alert skipped because ALERT_EMAIL_FROM (or ALERT_EMAIL_USERNAME) is not configured.")
        return False

    final_subject = format_alert_subject(
        subject=subject,
        platform=platform,
        include_platform=include_platform,
        include_account=include_account,
    )

    message = EmailMessage()
    message["Subject"] = final_subject
    message["From"] = from_address
    message["To"] = to_address
    message.set_content(body)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout_seconds) as smtp:
            smtp.ehlo()
            if use_tls:
                smtp.starttls()
                smtp.ehlo()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
        return True
    except Exception as error:
        logger.warning("Failed to send email alert: %s", error)
        return False


def send_failure_alert(
    *,
    app_name: str,
    error: Exception,
    context: str,
    clip_id: str | None = None,
    details: str = "",
    title: str = "",
    subject: str | None = None,
    platform: str = "",
) -> None:
    subject = subject or (f"Fail - {clip_id}" if clip_id else f"Fail - {app_name}")
    body = (
        f"App: {app_name}\n"
        f"Context: {context}\n"
        f"Error type: {type(error).__name__}\n"
        f"Error: {error}\n"
        f"{details}"
    )
    if title:
        body += f"\n\nDetails\n{title}\n"
    send_email_alert(app_name=app_name, subject=subject, body=body, platform=platform)
