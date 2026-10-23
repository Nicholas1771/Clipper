"""Append locally published clip IDs to a production profile's seen history (never removes IDs).

Local use (pushes this machine's seen IDs for the profile to production over SSH):
    python sync_seen_ids.py --carootclips [--dry-run]

Server side (invoked by the push over SSH; reads JSON clip IDs from standard input):
    python sync_seen_ids.py --carootclips --merge-stdin [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "Common" / "src"))

from common.env import available_accounts, load_app_env  # noqa: E402

CLIP_ID = re.compile(r"^\d{6,30}$")
DEFAULT_KEY = Path.home() / ".ssh" / "clipper-ec2.pem"
REMOTE_APP_DIR = "/home/ubuntu/Clipper"
REMOTE_PYTHON = "/home/ubuntu/venv/bin/python"


def discover_profiles() -> list[str]:
    return available_accounts(REPO_ROOT)


def resolve_state_file(profile: str) -> Path:
    os.environ["CLIPPER_PROFILE"] = profile
    load_app_env(REPO_ROOT / "apps" / "Common")
    raw = os.getenv("STATE_FILE", "data/state.json").strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def read_seen_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a seen-history object.")
    return {str(value) for value in payload.get("seen_ids", [])}


def valid_ids(values: object) -> set[str]:
    if not isinstance(values, list):
        raise ValueError("Expected a JSON list of clip IDs.")
    ids = {str(value).strip() for value in values}
    invalid = sorted(value for value in ids if not CLIP_ID.match(value))
    if invalid:
        raise ValueError(f"Rejected {len(invalid)} invalid clip ID(s).")
    return ids


@contextmanager
def file_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield


def merge_ids(state_file: Path, incoming: set[str], *, dry_run: bool = False) -> dict[str, int]:
    """Union incoming IDs into the seen history. Existing IDs and other keys are preserved."""
    with file_lock(state_file):
        payload: dict = {"seen_ids": []}
        if state_file.exists():
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"{state_file} is not a seen-history object; refusing to change it.")
            payload = loaded
        before = {str(value) for value in payload.get("seen_ids", [])}
        added = incoming - before
        result = {"before": len(before), "added": len(added), "after": len(before | incoming)}
        if dry_run or not added:
            return result
        merged = before | incoming
        if not before <= merged:
            raise RuntimeError("Merge would remove seen IDs; refusing to write.")
        payload["seen_ids"] = sorted(merged)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=state_file.name + ".", suffix=".tmp", dir=state_file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            if state_file.exists():
                os.chmod(tmp_name, state_file.stat().st_mode & 0o777)
            os.replace(tmp_name, state_file)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        if not before <= read_seen_ids(state_file):
            raise RuntimeError("Seen history verification failed after write.")
        return result


def production_host() -> str:
    host = os.getenv("PRODUCTION_HOST", "").strip()
    if not host:
        raise RuntimeError("Set PRODUCTION_HOST in the ignored root .env.")
    return host


def push(profile: str, *, dry_run: bool = False, timeout: float = 60) -> dict[str, int]:
    ids = read_seen_ids(resolve_state_file(profile))
    ids = {value for value in ids if CLIP_ID.match(value)}
    key = Path(os.getenv("PRODUCTION_SSH_KEY", str(DEFAULT_KEY))).expanduser()
    remote = f"cd {REMOTE_APP_DIR} && {REMOTE_PYTHON} sync_seen_ids.py --{profile} --merge-stdin" + (" --dry-run" if dry_run else "")
    command = [
        "ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=15", f"ubuntu@{production_host()}", remote,
    ]
    completed = subprocess.run(command, input=json.dumps(sorted(ids)), capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RuntimeError(f"Production merge failed (exit {completed.returncode}): {detail[-1] if detail else 'no output'}")
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    result["local"] = len(ids)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    for name in discover_profiles():
        group.add_argument(f"--{name}", dest="profile", action="store_const", const=name)
    parser.add_argument("--merge-stdin", action="store_true", help="Server side: merge JSON clip IDs from stdin into this machine's seen history.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be added without writing.")
    args = parser.parse_args(argv)

    if args.merge_stdin:
        result = merge_ids(resolve_state_file(args.profile), valid_ids(json.loads(sys.stdin.read() or "[]")), dry_run=args.dry_run)
        print(json.dumps(result))
        return 0

    result = push(args.profile, dry_run=args.dry_run)
    verb = "would add" if args.dry_run else "added"
    print(
        f"[{args.profile}] {result['local']} local seen ID(s); production {verb} {result['added']} "
        f"({result['before']} -> {result['after']})."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # concise CLI error, no traceback
        print(f"sync_seen_ids: {exc}", file=sys.stderr)
        sys.exit(1)
