# db.py
import os
import atexit
import psycopg2
from psycopg2.pool import ThreadedConnectionPool

# --- DSN / Pool sizing --------------------------------------------------------
# Prefer env; falls back to your hardcoded URL.
DEFAULT_DSN = (
    "postgresql://postgres.ubpprtdulbjulasrkupg:Invalid.55"
    "@aws-1-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require"
)
_DSN = os.getenv("DATABASE_URL", DEFAULT_DSN)

_MIN = int(os.getenv("DB_POOL_MIN", "1"))
_MAX = int(os.getenv("DB_POOL_MAX", "10"))

# Extra libpq connection options (timeouts + TCP keepalives)
_conn_kwargs = {
    "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    "keepalives": 1,
    "keepalives_idle": int(os.getenv("DB_KEEPALIVES_IDLE", "30")),
    "keepalives_interval": int(os.getenv("DB_KEEPALIVES_INTERVAL", "10")),
    "keepalives_count": int(os.getenv("DB_KEEPALIVES_COUNT", "3")),
}
# If sslmode isn't present in DSN, enforce it.
if "sslmode=" not in _DSN.lower():
    _conn_kwargs["sslmode"] = "require"

# One pool per process (e.g., per Gunicorn worker)
_POOL = ThreadedConnectionPool(
    minconn=_MIN,
    maxconn=_MAX,
    dsn=_DSN,
    **_conn_kwargs,
)


def _configure_session(conn):
    """Cheap per-checkout tweaks; ignore if not supported."""
    try:
        with conn.cursor() as cur:
            cur.execute("SET application_name = 'drip';")
            cur.execute("SET statement_timeout = 5000;")                    # 5s
            cur.execute("SET idle_in_transaction_session_timeout = 5000;")  # 5s
    except Exception:
        pass


class PooledConn:
    """
    Wrapper so:
      - `with get_connection() as conn:` works (commit/rollback)
      - `conn.close()` returns to pool (safe to call multiple times)
    """
    __slots__ = ("_pool", "_raw")

    def __init__(self, pool, raw):
        self._pool = pool
        self._raw = raw

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        self._raw.__enter__()  # psycopg2 handles txn begin
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._raw.__exit__(exc_type, exc, tb)  # commit/rollback
        finally:
            self.close()

    def close(self):
        if self._raw is not None:
            # ensure no open txn is left hanging
            try:
                self._raw.rollback()
            except Exception:
                pass
            self._pool.putconn(self._raw)
            self._raw = None


def get_connection():
    """
    Usage stays the same everywhere:
        with get_connection() as conn:
            ...
    or:
        conn = get_connection()
        try:
            with conn:
                ...
        finally:
            conn.close()
    """
    raw = _POOL.getconn()
    _configure_session(raw)
    return PooledConn(_POOL, raw)


def _close_pool():
    try:
        _POOL.closeall()
    except Exception:
        pass

atexit.register(_close_pool)