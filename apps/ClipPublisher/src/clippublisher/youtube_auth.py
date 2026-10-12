from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from clippublisher.config import AppConfig
from clippublisher.youtube_client import TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def _wait_for_code(server: HTTPServer, expected_state: str, timeout: int) -> str:
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            if query.get("state", [""])[0] == expected_state:
                result["code"] = query.get("code", [""])[0]
                result["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab and return to the terminal.")

        def log_message(self, *args: object) -> None:
            return

    server.RequestHandlerClass = Handler
    server.timeout = timeout
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    thread.join(timeout + 5)
    if result.get("error"):
        raise RuntimeError(f"Google returned an error: {result['error']}")
    if not result.get("code"):
        raise RuntimeError("No authorization code received (timed out or state mismatch).")
    return result["code"]


def run_youtube_auth(config: AppConfig, *, timeout_seconds: int = 300) -> None:
    if not config.youtube_client_id or not config.youtube_client_secret:
        raise RuntimeError("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET before running --youtube-auth.")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urlencode(
        {
            "client_id": config.youtube_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent select_account",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print("Opening your browser. Choose the Google account AND the YouTube channel this profile should publish to.")
    print(f"If it does not open, visit:\n{url}\n")
    webbrowser.open(url)
    try:
        code = _wait_for_code(server, state, timeout_seconds)
    finally:
        server.server_close()

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": config.youtube_client_id,
            "client_secret": config.youtube_client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({response.status_code}): {response.text[:300]}")
    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Google returned no refresh token. Revoke the app at myaccount.google.com/permissions and retry.")
    config.youtube_token_file.parent.mkdir(parents=True, exist_ok=True)
    config.youtube_token_file.write_text(json.dumps({"refresh_token": refresh_token}, indent=2), encoding="utf-8")
    print(f"Saved YouTube refresh token to {config.youtube_token_file}")
