from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?:\.trycloudflare\.com|\.cfargotunnel\.com)")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_local_queue_server(queue_dir: Path, port: int) -> ThreadingHTTPServer:
    queue_dir.mkdir(parents=True, exist_ok=True)
    handler = partial(SimpleHTTPRequestHandler, directory=str(queue_dir))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    atexit.register(server.shutdown)
    atexit.register(server.server_close)
    return server


def _read_cloudflare_url(process: subprocess.Popen[str]) -> str:
    if process.stdout is None:
        raise RuntimeError("cloudflared did not emit a URL because stdout was unavailable.")

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            time.sleep(0.25)
            continue

        match = _URL_RE.search(line)
        if match:
            return match.group(0).rstrip("/")

    raise RuntimeError("cloudflared did not provide a public URL in time.")


def _candidate_cloudflared_paths() -> list[str]:
   candidates: list[str] = []
   program_files = os.environ.get("ProgramFiles", "")
   program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
   manual_candidates = [
       shutil.which("cloudflared"),
       shutil.which("cloudflared.exe"),
       os.environ.get("CLOUDFLARED_PATH", ""),
   ]
   if program_files:
       manual_candidates.append(str(Path(program_files) / "cloudflared" / "cloudflared.exe"))
   if program_files_x86:
       manual_candidates.append(str(Path(program_files_x86) / "cloudflared" / "cloudflared.exe"))

   for raw in manual_candidates:
       if raw and Path(raw).exists():
           candidates.append(raw)

   local_app_data = Path(os.getenv("LOCALAPPDATA", "")).expanduser()
   winget_packages = local_app_data / "Microsoft" / "WinGet" / "Packages" if str(local_app_data) else None
   candidate_bases = []
   if program_files:
       candidate_bases.append(Path(program_files))
   if program_files_x86:
       candidate_bases.append(Path(program_files_x86))
   if winget_packages is not None:
       candidate_bases.insert(0, winget_packages)
   for base in candidate_bases:
       if base.exists():
           candidates.extend(str(path) for path in sorted(base.glob("**/cloudflared.exe")))

   unique: list[str] = []
   for candidate in candidates:
       if candidate not in unique:
           unique.append(candidate)
   return unique


def _ensure_cloudflared_available() -> str:
    for candidate in _candidate_cloudflared_paths():
        return candidate

    winget = shutil.which("winget")
    if winget:
        install = subprocess.run(
            [
                winget,
                "install",
                "--id",
                "Cloudflare.cloudflared",
                "-e",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ],
            capture_output=True,
            text=True,
        )
        if install.returncode == 0:
            for candidate in _candidate_cloudflared_paths():
                return candidate

    raise ValueError(
        "MEDIA_BASE_URL is required. ClipPublisher could not find Cloudflared and could not install it automatically. "
        "Install it with: winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements "
        "or set MEDIA_BASE_URL manually to a public HTTPS URL."
    )


class _ManagedTunnel:
    def __init__(self, queue_dir: Path, port: int, process: subprocess.Popen[str], url: str) -> None:
        self.queue_dir = queue_dir
        self.port = port
        self.process = process
        self.url = url


_tunnel: _ManagedTunnel | None = None
_managed_urls: set[str] = set()
_tunnel_lock = threading.Lock()


def _drain_output(process: subprocess.Popen[str]) -> None:
    # An unread stdout pipe eventually fills up and can stall cloudflared.
    try:
        for _ in process.stdout:
            pass
    except Exception:
        pass


def _start_tunnel(queue_dir: Path, port: int) -> _ManagedTunnel:
    cloudflared = _ensure_cloudflared_available()
    process = subprocess.Popen(
        [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    atexit.register(lambda: process.poll() is None and process.terminate())
    try:
        url = _read_cloudflare_url(process)
    except Exception:
        process.terminate()
        raise
    threading.Thread(target=_drain_output, args=(process,), daemon=True).start()
    return _ManagedTunnel(queue_dir, port, process, url)


def _tunnel_is_healthy(tunnel: _ManagedTunnel, *, attempts: int = 3) -> bool:
    for attempt in range(attempts):
        if tunnel.process.poll() is not None:
            return False
        try:
            response = requests.get(f"{tunnel.url}/", timeout=10)
            if response.status_code < 500:
                return True
        except requests.RequestException:
            pass
        if attempt + 1 < attempts:
            time.sleep(3)
    return False


def resolve_media_base_url(queue_dir: Path) -> str:
    global _tunnel
    configured = os.getenv("MEDIA_BASE_URL", "").strip().rstrip("/")
    if configured and configured not in _managed_urls:
        return configured

    with _tunnel_lock:
        port = _find_free_port()
        _start_local_queue_server(queue_dir, port)
        _tunnel = _start_tunnel(queue_dir, port)
        _managed_urls.add(_tunnel.url)
        os.environ["MEDIA_BASE_URL"] = _tunnel.url
        return _tunnel.url


def ensure_media_base_url(configured: str) -> str:
    """Return a reachable public base URL, restarting the managed tunnel if it has died.

    Manually configured URLs and R2 are returned untouched.
    """
    global _tunnel
    with _tunnel_lock:
        tunnel = _tunnel
        if tunnel is None or configured.rstrip("/") not in _managed_urls:
            return configured
        if _tunnel_is_healthy(tunnel):
            return tunnel.url

        logger.warning("Cloudflare tunnel %s is not reachable; restarting it.", tunnel.url)
        if tunnel.process.poll() is None:
            tunnel.process.terminate()
        for _ in range(3):
            candidate = _start_tunnel(tunnel.queue_dir, tunnel.port)
            if _tunnel_is_healthy(candidate, attempts=10):
                _tunnel = candidate
                _managed_urls.add(candidate.url)
                os.environ["MEDIA_BASE_URL"] = candidate.url
                logger.info("Cloudflare tunnel restarted: %s", candidate.url)
                return candidate.url
            candidate.process.terminate()
        raise RuntimeError("Could not restore a reachable Cloudflare tunnel for media hosting.")
