"""
settings.py
-----------
Central configuration loader for the Pakisa Mining Incentive System.
Reads config.yaml and exposes typed dataclasses.
Environment variables override any YAML value (12-factor app pattern).

.env: on import, this module loads <repo root>/.env (if present) into the process
environment via python-dotenv, before anything reads os.environ. Real OS/service-level
environment variables always win (load_dotenv's default override=False) — .env is only a
convenience for values nobody has set another way. See .env.example for the variables this
reads (DB_SERVER / DB_DATABASE / DB_DRIVER / DB_USERNAME / DB_PASSWORD) and how to point a
deployment at a different SQL Server instance without touching config.yaml or any code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "config.yaml"

load_dotenv(ROOT_DIR / ".env")


@dataclass
class DatabaseSettings:
    server: str = "localhost"
    database: str = "STPTM2000"
    username: str = ""
    password: str = ""
    use_windows_auth: bool = True
    driver: str = "ODBC Driver 17 for SQL Server"
    connection_timeout: int = 30
    command_timeout: int = 120
    pool_size: int = 5
    retry_attempts: int = 3
    retry_delay_seconds: int = 5

    def build_connection_string(self) -> str:
        server   = os.getenv("DB_SERVER",   self.server)
        database = os.getenv("DB_DATABASE", self.database)
        driver   = os.getenv("DB_DRIVER",   self.driver)

        if self.use_windows_auth and not os.getenv("DB_USERNAME"):
            return (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={database};"
                f"Trusted_Connection=yes;"
                f"Connection Timeout={self.connection_timeout};"
            )

        username = os.getenv("DB_USERNAME", self.username)
        password = os.getenv("DB_PASSWORD", self.password)
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={server};"
            f"DATABASE={database};"
            f"UID={username};"
            f"PWD={password};"
            f"Connection Timeout={self.connection_timeout};"
        )


@dataclass
class LoggingSettings:
    level: str = "INFO"
    log_dir: str = "logs"
    max_bytes: int = 10_485_760
    backup_count: int = 5
    format: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


@dataclass
class DataProcessingSettings:
    chunk_size: int = 10_000


@dataclass
class AppSettings:
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    data_processing: DataProcessingSettings = field(default_factory=DataProcessingSettings)


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(config_path: Optional[Path] = None) -> AppSettings:
    path = config_path or CONFIG_PATH
    raw: dict = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    db_raw  = raw.get("database", {})
    log_raw = raw.get("logging", {})
    dp_raw  = raw.get("data_processing", {})

    settings = AppSettings(
        database=DatabaseSettings(**db_raw),
        logging=LoggingSettings(**log_raw),
        data_processing=DataProcessingSettings(**dp_raw),
    )

    for d in [settings.logging.log_dir]:
        Path(ROOT_DIR / d).mkdir(parents=True, exist_ok=True)

    return settings


settings: AppSettings = load_settings()
