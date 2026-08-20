"""Logging setup for the Pakisa Mining Incentive System."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from config.settings import settings

_loggers: dict = {}


def get_logger(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    cfg = settings.logging
    root = Path(__file__).resolve().parent.parent.parent
    log_dir = root / cfg.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))

    if not logger.handlers:
        fmt = logging.Formatter(cfg.format)

        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

        fh = logging.handlers.RotatingFileHandler(
            log_dir / "pakisa.log",
            maxBytes=cfg.max_bytes,
            backupCount=cfg.backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.propagate = False
    _loggers[name] = logger
    return logger
