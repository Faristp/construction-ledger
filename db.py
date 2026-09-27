"""Postgres access layer. Swaps in for the old sqlite3 module with the same
db() context-manager shape, so app.py barely changes."""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Row

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Point it at your Postgres instance, e.g.\n"
        "  postgresql://user:password@host:5432/dbname\n"
        "On Render, add a Postgres database and it sets this for you automatically "
        "when the two services are linked."
    )
# Render (and Heroku-style hosts) hand out "postgres://" or "postgresql://";
# pin the driver explicitly to psycopg2 so SQLAlchemy doesn't guess.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)

SCHEMA = """
CREATE TABLE IF NOT EXISTS shareholders (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    notes   TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shareholders_name_ci ON shareholders (LOWER(name));

CREATE TABLE IF NOT EXISTS entries (
    id             SERIAL PRIMARY KEY,
    shareholder_id INTEGER NOT NULL REFERENCES shareholders(id) ON DELETE CASCADE,
    entry_date     DATE NOT NULL,
    purpose        TEXT NOT NULL,
    amount         BIGINT NOT NULL CHECK (amount >= 0)  -- stored in paise (1 INR = 100)
);
CREATE INDEX IF NOT EXISTS idx_entries_sh_date ON entries (shareholder_id, entry_date);

-- admin login sessions, kept in the DB (not memory) so a login survives
-- restarts and works even if the host runs more than one instance
CREATE TABLE IF NOT EXISTS admin_sessions (
    token      TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Cursor:
    """Thin wrapper so app.py's sqlite-style code (con.execute(...).fetchone(),
    row["col"], cur.rowcount, cur.lastrowid) keeps working unchanged."""

    def __init__(self, conn):
        self._conn = conn
        self._result = None

    def execute(self, sql: str, params=None):
        self._result = self._conn.execute(text(sql), params or {})
        return self

    def fetchone(self):
        row = self._result.fetchone()
        return dict(row._mapping) if row is not None else None

    def fetchall(self):
        return [dict(r._mapping) for r in self._result.fetchall()]

    @property
    def rowcount(self):
        return self._result.rowcount

    @property
    def lastrowid(self):
        row = self._result.fetchone()
        return row[0] if row else None


@contextmanager
def db():
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield Cursor(conn)
        trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with engine.begin() as conn:
        for statement in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            conn.execute(text(statement))
