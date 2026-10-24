from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from common.env import parse_bool


logger = logging.getLogger(__name__)


def render_alert_html(body: str, *, section_titles: set[str] | None = None) -> str:
    sections = {"Source", "Apify", "Gemini AI", "Editor"} | (section_titles or set())
    rows = []
    for line in body.splitlines():
        if not line:
            rows.append('<tr><td colspan="2" style="height:16px"></td></tr>')
        elif line in sections:
            rows.append(
                '<tr><th colspan="2" style="text-align:left;padding:10px 12px;'
                f'background:#eef2f6;font-size:17px">{escape(line)}</th></tr>'
            )
        else:
            label, separator, value = line.partition(": ")
            if not separator:
                rows.append(f'<tr><td colspan="2" style="padding:4px 12px">{escape(line)}</td></tr>')
                continue
            content = escape(value)
            if label == "Link":
                try:
                    url = urlsplit(value)
                    valid_link = url.scheme in {"http", "https"} and bool(url.netloc)
                except ValueError:
                    valid_link = False
                if valid_link:
                    content = f'<a href="{escape(value, quote=True)}" style="color:#0969da">{escape(value)}</a>'
            rows.append(
                '<tr><th style="text-align:left;vertical-align:top;padding:6px 12px;'
                f'width:34%;overflow-wrap:anywhere">{escape(label)}</th>'
                f'<td style="padding:6px 12px;overflow-wrap:anywhere">{content}</td></tr>'
            )
    return (
        '<!doctype html><html><body style="margin:0;padding:20px;background:#f6f8fa;'
        'color:#1f2328;font:15px/1.5 Arial,sans-serif">'
        '<table role="presentation" style="width:100%;max-width:760px;margin:auto;'
        'background:white;border:1px solid #d0d7de;border-collapse:collapse">'
        + "".join(rows) + "</table></body></html>"
    )


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
    if re.fullmatch(r"Clip (Plan|Start|Report) \d{8} (PROD|LOCAL)( TEST)?", rest):
        return rest
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

    publish_outcome = re.match(r"^(Success|Fail)\b", rest, flags=re.IGNORECASE)
    if publish_outcome:
        outcome = publish_outcome.group(1).capitalize()
        return f"Clip {outcome} {plat} {account} {env}"

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
    html_body: str | None = None,
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
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")

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


def send_scheduled_failure_alert(*, queue_dir: Path, stage: str, error_type: str, details: str) -> bool:
    try:
        state = json.loads((queue_dir / "scheduler_state.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        logger.warning("Could not read scheduled slot details for the %s failure alert: %s", stage, error)
        return False
    if not isinstance(state, dict) or not state.get("scheduled_for"):
        logger.warning("Could not send the %s failure alert without scheduled slot details.", stage)
        return False

    from common.platforms import PLATFORM_LABELS, normalize_platforms

    platforms = normalize_platforms(state.get("platforms"))
    platform_text = ", ".join(PLATFORM_LABELS[platform] for platform in platforms)
    platform = PLATFORM_LABELS[platforms[0]] if len(platforms) == 1 else ""
    creator = str(state.get("creator_name") or "").strip()
    lines = [
        "Publish Failure",
        f"Stage: {stage}",
        f"Scheduled for: {state['scheduled_for']}",
        f"Platforms: {platform_text}",
    ]
    if creator:
        lines.append(f"Creator: {creator}")
    lines.extend(["", "Error", f"Type: {error_type}", f"Details: {details.strip()}"])
    body = "\n".join(lines) + "\n"
    html_body = render_alert_html(body, section_titles={"Publish Failure", "Error"})
    return send_email_alert(
        app_name=stage,
        subject=f"Fail - {stage}",
        body=body,
        html_body=html_body,
        platform=platform,
    )


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
    from common.creators import active_creator_name

    creator = active_creator_name.get()
    creator_text = f"Creator: {creator}\n" if creator else ""
    subject = subject or (f"Fail - {clip_id}" if clip_id else f"Fail - {app_name}")
    body = (
        f"App: {app_name}\n"
        f"{creator_text}"
        f"Context: {context}\n"
        f"Error type: {type(error).__name__}\n"
        f"Error: {error}\n"
        f"{details}"
    )
    if title:
        body += f"\n\nDetails\n{title}\n"
    send_email_alert(app_name=app_name, subject=subject, body=body, platform=platform)
