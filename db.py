# db.py (improved)
import os
import atexit
import time
import logging
from typing import Optional

import psycopg2
from psycopg2.pool import ThreadedConnectionPool, PoolError

log = logging.getLogger("drip.db")
logging.basicConfig(level=os.getenv("DB_LOG_LEVEL", "INFO"))

# --- DSN / Pool sizing --------------------------------------------------------
DEFAULT_DSN = (
    "postgresql://postgres.ubpprtdulbjulasrkupg:Invalid.55"
    "@aws-1-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require"
)
_DSN = os.getenv("DATABASE_URL", DEFAULT_DSN)

_MIN = int(os.getenv("DB_POOL_MIN", "1"))
_MAX = int(os.getenv("DB_POOL_MAX", "10"))

# connection timeouts & keepalive (configurable)
_CONN_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))  # raising to 10s by default
_CONN_KP_IDLE = int(os.getenv("DB_KEEPALIVES_IDLE", "30"))
_CONN_KP_INT = int(os.getenv("DB_KEEPALIVES_INTERVAL", "10"))
_CONN_KP_COUNT = int(os.getenv("DB_KEEPALIVES_COUNT", "3"))

_conn_kwargs = {
    "connect_timeout": _CONN_TIMEOUT,
    "keepalives": 1,
    "keepalives_idle": _CONN_KP_IDLE,
    "keepalives_interval": _CONN_KP_INT,
    "keepalives_count": _CONN_KP_COUNT,
}
if "sslmode=" not in _DSN.lower():
    _conn_kwargs["sslmode"] = "require"

# Lazy pool: create on first request (avoids problems with forking/serverless).
_POOL: Optional[ThreadedConnectionPool] = None


def _create_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    try:
        log.info("Creating DB pool min=%s max=%s", _MIN, _MAX)
        _POOL = ThreadedConnectionPool(minconn=_MIN, maxconn=_MAX, dsn=_DSN, **_conn_kwargs)
        return _POOL
    except Exception:
        log.exception("Failed to create DB pool")
        raise


def _configure_session(conn):
    try:
        if getattr(conn, "_drip_configured", False):
            return
        with conn.cursor() as cur:
            cur.execute("SET application_name = 'drip';")
            # short server-side statement timeouts to fail fast
            cur.execute("SET statement_timeout = 5000;")                    # 5s
            cur.execute("SET idle_in_transaction_session_timeout = 5000;")  # 5s
        conn._drip_configured = True
    except Exception:
        # non-fatal; we'll try again on next checkout
        log.debug("Session configuration failed (non-fatal)", exc_info=True)


class PooledConn:
    __slots__ = ("_pool", "_raw")
    def __init__(self, pool, raw):
        self._pool = pool
        self._raw = raw

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._raw.__exit__(exc_type, exc, tb)
        finally:
            self.close()

    def close(self):
        if self._raw is not None:
            try:
                self._raw.rollback()
            except Exception:
                pass
            try:
                self._pool.putconn(self._raw)
            except Exception:
                log.exception("putconn failed; closing raw connection")
                try:
                    self._raw.close()
                except Exception:
                    pass
            self._raw = None


def get_connection(retries: int = 3, backoff: float = 0.2):
    """
    Return PooledConn. Retries if pool is exhausted.
    """
    pool = _create_pool()
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            raw = pool.getconn()
            _configure_session(raw)
            return PooledConn(pool, raw)
        except PoolError as e:
            last_exc = e
            log.warning("Pool exhausted on attempt %d/%d: %s", attempt, retries, e)
            time.sleep(backoff * attempt)
        except psycopg2.OperationalError as e:
            last_exc = e
            log.warning("OperationalError on attempt %d/%d: %s", attempt, retries, e)
            time.sleep(backoff * attempt)
    # final fallback: raise the last exception with hint
    log.error("Unable to get a DB connection after %d attempts", retries)
    raise last_exc or RuntimeError("Failed to get DB connection")


def _close_pool():
    global _POOL
    if _POOL is None:
        return
    try:
        log.info("Closing DB pool")
        _POOL.closeall()
    except Exception:
        log.exception("Error closing DB pool")
    finally:
        _POOL = None


atexit.register(_close_pool)
