from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys
import time

APP_ROOT = Path(__file__).resolve().parent
SRC_ROOT = APP_ROOT / "src"
COMMON_SRC_ROOT = APP_ROOT.parent / "Common" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(COMMON_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_SRC_ROOT))

from clipscheduler.config import AppConfig
from clipscheduler.runner import STAGES, ClipSchedulerRunner
from common.alerts import send_failure_alert
from common.env import is_test_mode
from common.logging_utils import configure_app_logging

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Schedule the Clip pipeline based on a configurable window.")
    parser.add_argument("--now", action="store_true", help="Immediately mark the queue ready for sourcing, then keep running.")
    parser.add_argument(
        "--stop-after",
        choices=STAGES,
        help="Fire a cycle immediately without archiving the queue, and exit once this stage completes.",
    )
    return parser.parse_args()


def run_until_stage(runner: ClipSchedulerRunner, stage: str) -> int:
    runner.reset_state_files()
    started = runner._local_now()
    runner._mark_ready(when=started)
    logger.info("ClipScheduler fired one cycle; waiting for stage '%s' to complete.", stage)
    while True:
        result = runner.stage_status(stage)
        if result == "reached":
            logger.info("Stage '%s' complete. Exiting so the pipeline shuts down.", stage)
            return 0
        if result == "failed":
            logger.error("Sourcing failed before stage '%s' was reached.", stage)
            return 1
        time.sleep(min(runner.config.poll_interval_seconds, 2))


def main() -> int:
    configure_app_logging("ClipScheduler")
    try:
        args = parse_args()
        runner = ClipSchedulerRunner(AppConfig.from_repo_root(APP_ROOT))
        if args.stop_after:
            return run_until_stage(runner, args.stop_after)
        if is_test_mode():
            runner.reset_state_files()
        else:
            runner.prepare_startup()
        if args.now:
            logger.info("ClipScheduler activated immediately: %s", runner.fire_now())
        runner.run_forever()
        return 0
    except KeyboardInterrupt:
        logger.info("Shutting down ClipScheduler.")
        return 0
    except Exception as error:
        logger.exception("ClipScheduler failed: %s", error)
        send_failure_alert(app_name="ClipScheduler", error=error, context="runtime failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
