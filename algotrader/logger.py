from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


class ISTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def setup_logger(level: str) -> logging.Logger:
    logger = logging.getLogger("algotrader")
    if logger.handlers:
        logger.setLevel(level.upper())
        return logger

    logger.setLevel(level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(
        ISTFormatter("%(asctime)s IST | %(levelname)s | %(message)s")
    )
    logger.addHandler(handler)
    return logger
