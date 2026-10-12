from __future__ import annotations

import argparse
import logging
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
    return parser.parse_args()


def main() -> int:
    configure_logging()
    try:
        args = parse_args()
        repo_root = APP_ROOT
        config = AppConfig.from_repo_root(repo_root, resolve_media_url=not (args.refresh_token or args.youtube_auth))
        runner = QueuePublisherRunner(config)

        if args.youtube_auth:
            run_youtube_auth(config)
            return 0

        if args.refresh_token:
            resolved = resolve_publish_access_token(config, force_refresh=True)
            logger.info("Token refresh complete. Active token suffix: ...%s", resolved[-8:] if len(resolved) >= 8 else resolved)
            return 0

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
