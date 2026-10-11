from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: str = "logs") -> logging.Logger:
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("positionx")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    fh = TimedRotatingFileHandler(path / "positionx.log", when="midnight", backupCount=30, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
