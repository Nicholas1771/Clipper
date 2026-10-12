from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
SRC_ROOT = APP_ROOT / "src"
COMMON_SRC_ROOT = APP_ROOT.parent / "Common" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(COMMON_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_SRC_ROOT))

from clipai.config import AppConfig
from clipai.service import ClipAIService
from common.alerts import send_failure_alert
from common.logging_utils import configure_app_logging

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    configure_app_logging("ClipAI")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze sourced clips for KICK overlays and write SEO summaries before editing.")
    parser.add_argument("--once", action="store_true", help="Run one AI analysis pass and exit.")
    return parser.parse_args()


def main() -> int:
    configure_logging()
    try:
        args = parse_args()
        config = AppConfig.from_repo_root(APP_ROOT)
        service = ClipAIService(config)

        if args.once:
            service.process_once()
            return 0

        service.run_forever()
        return 0
    except KeyboardInterrupt:
        logger.info("Shutting down ClipAI.")
        return 0
    except Exception as error:
        logger.exception("ClipAI failed: %s", error)
        send_failure_alert(app_name="ClipAI", error=error, context="runtime failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
