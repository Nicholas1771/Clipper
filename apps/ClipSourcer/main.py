from __future__ import annotations

import argparse
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

from clipsourcer.config import AppConfig
from clipsourcer.service import ClipSourcerService
from clipsourcer.storage import save_seen_ids
from common.alerts import send_failure_alert
from common.env import is_test_mode
from common.logging_utils import configure_app_logging

logger = logging.getLogger(__name__)

def configure_logging() -> None:
    configure_app_logging("ClipSourcer")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll TikTok for recent viral clip candidates.")
    parser.add_argument("--once", action="store_true", help="Run a single sourcing cycle and exit.")
    parser.add_argument("--test", action="store_true", help="Test mode: work inside <QUEUE_DIR>/Test and use its source folder instead of sourcing a new clip.")
    parser.add_argument("--clear", action="store_true", help="Reset the ClipSourcer seen IDs to empty and exit.")
    return parser.parse_args()


def main() -> int:
    configure_logging()
    try:
        args = parse_args()
        if args.test:
            os.environ["CLIPPER_TEST_MODE"] = "1"
        repo_root = APP_ROOT
        config = AppConfig.from_repo_root(repo_root)
        service = ClipSourcerService(config, test_mode=is_test_mode())

        if args.clear:
            save_seen_ids(config.state_file, set())
            logger.info("Cleared ClipSourcer seen IDs at %s.", config.state_file)
            if args.once:
                outcome = service.run_once(bypass_schedule=True)
                if outcome.status == "sourced" and outcome.result is not None:
                    logger.info("Sourced manifest: %s", outcome.result.manifest_path)
                    if outcome.result.downloaded_video_path is not None:
                        logger.info("Downloaded video: %s", outcome.result.downloaded_video_path)
                elif outcome.reason:
                    logger.info("No queued clip created in this run: %s", outcome.reason)
                else:
                    logger.info("No queued clip created in this run.")
                return 0
            service.run_forever()
            return 0

        if args.once:
            outcome = service.run_once(bypass_schedule=True)
            if outcome.status == "sourced" and outcome.result is not None:
                logger.info("Sourced manifest: %s", outcome.result.manifest_path)
                if outcome.result.downloaded_video_path is not None:
                    logger.info("Downloaded video: %s", outcome.result.downloaded_video_path)
            elif outcome.reason:
                logger.info("No queued clip created in this run: %s", outcome.reason)
            else:
                logger.info("No queued clip created in this run.")
            return 0

        service.run_forever()
        return 0
    except KeyboardInterrupt:
        logger.info("Shutting down ClipSourcer.")
        return 0
    except Exception as error:
        logger.exception("ClipSourcer failed: %s", error)
        send_failure_alert(app_name="ClipSourcer", error=error, context="runtime failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
