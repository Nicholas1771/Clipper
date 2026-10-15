from __future__ import annotations

import argparse
import getpass
import logging
import os
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parent
SRC_ROOT = APP_ROOT / "src"
COMMON_SRC_ROOT = APP_ROOT.parent / "Common" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(COMMON_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_SRC_ROOT))

from clippublisher.config import AppConfig
from clippublisher.runner import QueuePublisherRunner
from clippublisher.token_manager import resolve_publish_access_token
from clippublisher.youtube_auth import run_youtube_auth
from common.alerts import send_failure_alert
from common.logging_utils import configure_app_logging


logger = logging.getLogger(__name__)


def configure_logging() -> None:
    configure_app_logging("ClipPublisher")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch a queue of sourced clips and publish ready items to Instagram.")
    parser.add_argument("--once", action="store_true", help="Process one ready queue item and exit.")
    parser.add_argument("--refresh-token", action="store_true", help="Force-refresh and persist the long-lived Instagram token, then exit.")
    parser.add_argument("--youtube-auth", action="store_true", help="Run the one-time YouTube OAuth login for the active profile, then exit.")
    profiles = parser.add_mutually_exclusive_group()
    for path in sorted((APP_ROOT.parent.parent / "profiles").glob("*.env")):
        if not path.stem.endswith(".local"):
            profiles.add_argument(f"--{path.stem}", dest="profile", action="store_const", const=path.stem)
    tokens = parser.add_mutually_exclusive_group()
    tokens.add_argument("--token", help="Replacement Instagram token; requires --refresh-token. Prefer --token-prompt to avoid shell history.")
    tokens.add_argument("--token-prompt", action="store_true", help="Read a replacement Instagram token using a hidden prompt.")
    tokens.add_argument("--token-stdin", action="store_true", help="Read a replacement Instagram token from standard input (for automated rotation).")
    args = parser.parse_args()
    if (args.token is not None or args.token_prompt or args.token_stdin) and not args.refresh_token:
        parser.error("--token, --token-prompt and --token-stdin require --refresh-token")
    if args.refresh_token and (args.once or args.youtube_auth):
        parser.error("--refresh-token cannot be combined with --once or --youtube-auth")
    if args.token is not None and not args.token.strip():
        parser.error("--token must not be empty")
    return args


def main() -> int:
    configure_logging()
    try:
        args = parse_args()
        if args.profile:
            os.environ["CLIPPER_PROFILE"] = args.profile
        repo_root = APP_ROOT
        config = AppConfig.from_repo_root(repo_root, resolve_media_url=not (args.refresh_token or args.youtube_auth))

        if args.youtube_auth:
            run_youtube_auth(config)
            return 0

        if args.refresh_token:
            from clippublisher.token_manager import replace_access_token

            if args.token_stdin:
                replacement = sys.stdin.readline().strip()
                if not replacement:
                    raise ValueError("No token received on standard input.")
            elif args.token_prompt:
                replacement = getpass.getpass("New Instagram access token: ")
            else:
                replacement = args.token
            if replacement is not None:
                replace_access_token(config, replacement)
            else:
                resolve_publish_access_token(config, force_refresh=True)
            logger.info("Token refresh complete for profile %s. Saved to %s.", os.environ.get("CLIPPER_PROFILE"), config.token_state_file)
            return 0

        runner = QueuePublisherRunner(config)
        if args.once:
            runner.process_one_ready_item(bypass_schedule=True)
            return 0

        runner.run_forever()
        return 0
    except KeyboardInterrupt:
        logger.info("Shutting down ClipPublisher.")
        return 0
    except Exception as error:
        logger.exception("ClipPublisher failed: %s", error)
        if not getattr(error, "_clipper_alert_sent", False):
            send_failure_alert(app_name="ClipPublisher", error=error, context="runtime failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
