from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
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
    parser.add_argument("--schedule", action="store_true", help="Run the start-of-day plan creation (as at 00:00), including YouTube selection, then exit without sourcing or publishing.")
    parser.add_argument("--schedule-date", type=date.fromisoformat, help="Day (YYYY-MM-DD) to initialize with --schedule; default is tomorrow, the next production run.")
    parser.add_argument(
        "--stop-after",
        choices=STAGES,
        help="Archive existing queue items, fire a fresh cycle immediately, and exit once this stage completes.",
    )
    args = parser.parse_args()
    if args.schedule and (args.now or args.stop_after):
        parser.error("--schedule cannot be combined with --now or --stop-after")
    if args.schedule_date and not args.schedule:
        parser.error("--schedule-date requires --schedule")
    return args


def run_until_stage(runner: ClipSchedulerRunner, stage: str) -> int:
    runner.force_reset_current_cycle()
    started = runner._local_now()
    runner._mark_ready(when=started)
    logger.info("ClipScheduler fired one cycle; waiting for stage '%s' to complete.", stage)
    while True:
        result = runner.stage_status(stage)
        if result == "reached":
            logger.info("Stage '%s' complete. Exiting so the pipeline shuts down.", stage)
            return 0
        if result == "failed":
            logger.error("Pipeline failed before stage '%s' was reached.", stage)
            return 1
        time.sleep(min(runner.config.poll_interval_seconds, 2))


def main() -> int:
    configure_app_logging("ClipScheduler")
    try:
        args = parse_args()
        if args.schedule and is_test_mode():
            raise ValueError("--schedule requires normal account configuration, without test mode.")
        runner = ClipSchedulerRunner(AppConfig.from_repo_root(APP_ROOT))
        if args.schedule:
            local_now = runner._local_now()
            day = args.schedule_date or local_now.date() + timedelta(days=1)
            start_of_day = datetime.combine(day, datetime.min.time(), local_now.tzinfo)
            logger.info("SIMULATION (no plan is saved, no alerts, sourcing or publishing; paid AI usage is journaled): start-of-day schedule for %s at %s.", day, start_of_day.isoformat())
            plan, youtube = runner.simulate_day(start_of_day)
            for index, slot in enumerate(plan.slots):
                logger.info("  Slot %s: %s [%s] %s", index + 1, slot.strftime("%Y-%m-%d %H:%M:%S %Z"), "+".join(plan.platforms_for(index)) or "none", plan.creator_names[index] if plan.creator_names else "")
            for slot in plan.youtube_slots:
                logger.info("  YouTube slot: %s", slot.strftime("%Y-%m-%d %H:%M:%S %Z"))
            if not plan.youtube_slots:
                logger.info("YouTube selection: no YouTube slots for this configuration.")
            elif youtube is None:
                logger.warning("YouTube selection: could not be completed (see errors above); production would retry every five minutes until the window closes.")
            else:
                logger.info("YouTube selection from Instagram posts on %s:", youtube["source_date"])
                for entry in youtube["entries"]:
                    logger.info("  #%s %s (IG media %s): %s views -> YouTube at %s", entry["rank"], entry["clip_id"], entry["media_id"], entry["views"], entry["scheduled_for"])
                if youtube["unscheduled"]:
                    logger.info("  Ranked but not scheduled: %s", ", ".join(youtube["unscheduled"]))
                for item in youtube["excluded"]:
                    logger.info("  Excluded %s: %s", item.get("clip_id"), item.get("reason"))
                if not youtube["entries"]:
                    logger.info("  No eligible Instagram posts; no YouTube upload would be scheduled.")
            logger.info("Simulation complete: %s pipeline slot(s), %s YouTube slot(s). Only paid AI usage accounting may be written.", len(plan.slots), len(plan.youtube_slots))
            return 0
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
