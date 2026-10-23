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

from clipeditor.config import AppConfig
from clipeditor.service import ClipEditorService
from common.alerts import send_failure_alert
from common.logging_utils import configure_app_logging
from common.reporting import record_stage_failure

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    configure_app_logging("ClipEditor")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edit sourced clips with logo overlays and metadata files.")
    parser.add_argument("--once", action="store_true", help="Process one ready queue item and exit.")
    return parser.parse_args()


def main() -> int:
    configure_logging()
    config = None
    try:
        args = parse_args()
        config = AppConfig.from_repo_root(APP_ROOT)
        service = ClipEditorService(config)

        if args.once:
            service.process_once()
            return 0
        service.run_forever()
        return 0
    except KeyboardInterrupt:
        logger.info("Shutting down ClipEditor.")
        return 0
    except Exception as error:
        logger.exception("ClipEditor failed: %s", error)
        if config is not None:
            record_stage_failure(config.queue_dir, "ClipEditor", error)
        send_failure_alert(app_name="ClipEditor", error=error, context="runtime failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
