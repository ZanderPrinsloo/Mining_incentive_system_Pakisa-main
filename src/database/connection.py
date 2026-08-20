"""
connection.py
-------------
SQL Server connection manager using SQLAlchemy + pyodbc for the Pakisa system.
Database: STPTM2000
"""
from __future__ import annotations

import time
import urllib.parse
from contextlib import contextmanager
from threading import Lock
from typing import Generator, Iterator, Optional

import pandas as pd
import pyodbc
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from config.settings import settings
from src.utils.logger import get_logger

log = get_logger(__name__)

_engine: Optional[Engine] = None
_engine_lock = Lock()


def _get_engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            conn_str = settings.database.build_connection_string()
            params   = urllib.parse.quote_plus(conn_str)
            url      = f"mssql+pyodbc:///?odbc_connect={params}"
            _engine  = create_engine(
                url,
                pool_size=settings.database.pool_size,
                pool_pre_ping=True,
                pool_recycle=3600,
                connect_args={"timeout": settings.database.connection_timeout},
                fast_executemany=True,
            )
            log.info("SQLAlchemy engine initialised (pool_size=%d)", settings.database.pool_size)
    return _engine


class _ConnectionPool:
    def __init__(self, conn_str: str, pool_size: int = 5, timeout: int = 30) -> None:
        self._conn_str  = conn_str
        self._pool_size = pool_size
        self._timeout   = timeout
        self._pool: list = []
        self._lock = Lock()

    def _new_connection(self) -> pyodbc.Connection:
        conn = pyodbc.connect(self._conn_str, timeout=self._timeout)
        conn.autocommit = False
        return conn

    def acquire(self) -> pyodbc.Connection:
        with self._lock:
            if self._pool:
                conn = self._pool.pop()
                try:
                    conn.execute("SELECT 1")
                    return conn
                except Exception:
                    pass
            return self._new_connection()

    def release(self, conn: pyodbc.Connection) -> None:
        with self._lock:
            if len(self._pool) < self._pool_size:
                try:
                    conn.rollback()
                    self._pool.append(conn)
                    return
                except Exception:
                    pass
        try:
            conn.close()
        except Exception:
            pass


_pool: Optional[_ConnectionPool] = None
_pool_lock = Lock()


def _get_pool() -> _ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            conn_str = settings.database.build_connection_string()
            _pool = _ConnectionPool(
                conn_str,
                pool_size=settings.database.pool_size,
                timeout=settings.database.connection_timeout,
            )
    return _pool


@contextmanager
def get_connection() -> Generator[pyodbc.Connection, None, None]:
    pool = _get_pool()
    db   = settings.database
    conn: Optional[pyodbc.Connection] = None
    for attempt in range(1, db.retry_attempts + 1):
        try:
            conn = pool.acquire()
            yield conn
            conn.commit()
            return
        except pyodbc.Error as exc:
            log.warning("DB error attempt %d/%d: %s", attempt, db.retry_attempts, exc)
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
                conn = None
            if attempt < db.retry_attempts:
                time.sleep(db.retry_delay_seconds)
        finally:
            if conn is not None:
                pool.release(conn)
    raise ConnectionError(f"Could not connect after {db.retry_attempts} attempts.")


def read_sql(query: str, params: Optional[tuple] = None) -> pd.DataFrame:
    """Execute a SELECT and return a DataFrame via SQLAlchemy."""
    engine = _get_engine()
    log.debug("Executing query (first 300 chars):\n%s", query[:300])
    with engine.connect() as conn:
        if params:
            result = pd.read_sql(text(query), conn, params=params)
        else:
            result = pd.read_sql(text(query), conn)
    return result


def read_sql_chunked(
    query: str,
    chunk_size: Optional[int] = None,
    params: Optional[tuple] = None,
) -> Iterator[pd.DataFrame]:
    chunk_size = chunk_size or settings.data_processing.chunk_size
    engine = _get_engine()
    with engine.connect() as conn:
        for chunk in pd.read_sql(text(query), conn, chunksize=chunk_size):
            yield chunk


def test_connection() -> bool:
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT GETDATE() AS server_time"))
            row    = result.fetchone()
            log.info("Connection test passed. Server time: %s", row[0])
        return True
    except Exception as exc:
        log.error("Connection test FAILED: %s", exc)
        return False
