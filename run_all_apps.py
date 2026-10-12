from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APPS = ("scheduler", "sourcer", "ai", "editor", "publisher")
APP_DIRS = {
    "scheduler": "ClipScheduler",
    "sourcer": "ClipSourcer",
    "ai": "ClipAI",
    "editor": "ClipEditor",
    "publisher": "ClipPublisher",
}


def discover_profiles() -> list[str]:
    """Every profiles/<name>.env is a runnable profile; adding a file adds a --<name> flag."""
    profiles_dir = Path(__file__).resolve().parent / "profiles"
    return sorted(path.stem.lower() for path in profiles_dir.glob("*.env"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch ClipScheduler -> ClipSourcer -> ClipAI -> ClipEditor -> ClipPublisher as separate processes."
    )
    profile_group = parser.add_mutually_exclusive_group()
    profiles = discover_profiles()
    for name in profiles:
        profile_group.add_argument(f"--{name}", dest="profile", action="store_const", const=name, help=f"Run the {name} profile (profiles\\{name}.env).")
    default_profile = os.getenv("CLIPPER_PROFILE", "").strip().lower()
    if not default_profile:
        default_profile = profiles[0] if profiles else "default"
    parser.set_defaults(profile=default_profile)
    parser.add_argument("--now", action="store_true", help="Fire the first cycle immediately, then run as normal.")
    parser.add_argument(
        "--stop-after",
        choices=APPS,
        help="Run one cycle immediately through this app without archiving the queue, then shut everything down.",
    )
    parser.add_argument(
        "--publish",
        choices=("instagram", "youtube"),
        help="Publish only to this platform (default: whatever the schedule assigns to each slot).",
    )
    parser.add_argument("--test", action="store_true", help="Run everything inside <QUEUE_DIR>/Test, using its source folder instead of sourcing a new clip.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without launching.")
    return parser.parse_args()


def build_launch_plan(repo_root: Path, args: argparse.Namespace) -> list[tuple[str, Path, list[str]]]:
    apps = APPS[: APPS.index(args.stop_after) + 1] if args.stop_after else APPS
    plan = []
    for app in apps:
        flags: list[str] = []
        if app == "scheduler":
            if args.stop_after:
                flags = ["--stop-after", args.stop_after]
            elif args.now:
                flags = ["--now"]
        plan.append((APP_DIRS[app], repo_root / "apps" / APP_DIRS[app], flags))
    return plan


def terminate_children(children: list[subprocess.Popen]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.time() + 8
    while time.time() < deadline:
        if all(child.poll() is not None for child in children):
            return
        time.sleep(0.2)
    for child in children:
        if child.poll() is None:
            child.kill()


def launch_processes(plan: list[tuple[str, Path, list[str]]], stop_mode: bool, test_mode: bool = False, profile: str = "default", publish_only: str | None = None) -> int:
    children: list[subprocess.Popen] = []
    env = dict(os.environ, CLIPPER_PROFILE=profile)
    if test_mode:
        env["CLIPPER_TEST_MODE"] = "1"
    if publish_only:
        env["CLIPPER_PUBLISH_ONLY"] = publish_only
        print(f"[launcher] publishing only to: {publish_only}")
    else:
        env.pop("CLIPPER_PUBLISH_ONLY", None)
    print(f"[launcher] profile: {profile}")
    try:
        for name, dir_path, flags in plan:
            command = [sys.executable, "main.py", *flags]
            print(f"[launcher] starting {name}: {' '.join(command)}")
            children.append(subprocess.Popen(command, cwd=dir_path, env=env))
            time.sleep(1.0)

        scheduler = children[0]
        while True:
            for child in children:
                code = child.poll()
                if code not in (None, 0):
                    print(f"[launcher] {child.args} exited with code {code}. Stopping remaining apps.")
                    terminate_children(children)
                    return code
            if stop_mode and scheduler.poll() == 0:
                print("[launcher] target stage complete. Shutting down all apps.")
                terminate_children(children)
                return 0
            if all(child.poll() is not None for child in children):
                return 0
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("[launcher] interrupt received. Stopping all apps.")
        terminate_children(children)
        return 0


def main() -> int:
    args = parse_args()
    plan = build_launch_plan(Path(__file__).resolve().parent, args)
    if args.dry_run:
        for name, dir_path, flags in plan:
            print(f"[dry-run] [{args.profile}] {name} @ {dir_path}: main.py {' '.join(flags)}".rstrip())
        return 0
    return launch_processes(plan, stop_mode=bool(args.stop_after), test_mode=args.test, profile=args.profile, publish_only=args.publish)


if __name__ == "__main__":
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda _sig, _frame: sys.exit(0))
    raise SystemExit(main())
