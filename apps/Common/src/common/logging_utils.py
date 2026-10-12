from __future__ import annotations

import copy
import logging


class AppNameFormatter(logging.Formatter):
    def __init__(self, app_name: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.app_name = app_name

    def format(self, record: logging.LogRecord) -> str:
        record = copy.copy(record)
        record.app = self.app_name
        return super().format(record)


def configure_app_logging(app_name: str) -> None:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    root_logger.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(AppNameFormatter(app_name, "%(asctime)s | %(levelname)s | %(app)s | %(message)s"))
    root_logger.addHandler(handler)
