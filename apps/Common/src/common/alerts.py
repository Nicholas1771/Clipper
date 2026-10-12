from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from common.env import parse_bool


logger = logging.getLogger(__name__)

def send_email_alert(*, app_name: str, subject: str, body: str, platform: str = "") -> bool:
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

    account = os.getenv("ACCOUNT_NAME", "").strip()
    rest = subject.strip()
    for prefix in (platform, "Clipper", account):
        if prefix and rest.lower().startswith(prefix.lower() + " "):
            rest = rest[len(prefix):].strip()
    subject = " ".join(part for part in (platform or "Clipper", account, rest) if part)

    message = EmailMessage()
    message["Subject"] = subject
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
    subject = subject or (f"Clipper Fail - {clip_id}" if clip_id else f"Clipper Fail - {app_name}")
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
