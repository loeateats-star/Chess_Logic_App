"""Shared PostgreSQL connection. Kept out of app.py so game_analysis.py doesn't
need to import app.py (which caused a circular import when app.py is run
directly as __main__ — that execution path re-imports itself under the name
"app", re-triggering the `from game_analysis import games_bp` line while
game_analysis is still mid-import).

connect() returns a thin wrapper around a psycopg2 connection that mimics the
slice of the sqlite3.Connection API the rest of the app was written against —
conn.execute(...)/conn.executescript(...) with '?' placeholders, dict-style
row access, and cursor.lastrowid — so routes written for SQLite didn't need
to be rewritten statement-by-statement for the Postgres migration.
"""
import os

import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError(
        'DATABASE_URL environment variable is not set. '
        'Set it to a PostgreSQL connection string, e.g. '
        'postgres://user:password@host:5432/dbname'
    )

# Alias so callers can keep writing `except db.IntegrityError:` the same way
# they wrote `except sqlite3.IntegrityError:`.
IntegrityError = psycopg2.IntegrityError

# Every route was opening a brand-new TCP+TLS+auth connection to Postgres
# and tearing it down again — that handshake (worse yet against a
# serverless host like Neon) was the single biggest per-request latency
# cost in the app, paid even by routes that run one cheap SELECT. A pool
# keeps a handful of connections open and hands them out instead.
# `minconn` opens eagerly at import time; `maxconn` is sized comfortably
# above gunicorn's --threads count (see Procfile) so concurrent requests
# don't contend for a connection.
_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)


class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace('?', '%s'), params)
        return self

    def executemany(self, sql, seq_of_params):
        self._cur.executemany(sql.replace('?', '%s'), seq_of_params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        """Only valid when the statement ended with `RETURNING id`
        (psycopg2 has no automatic equivalent of sqlite3's lastrowid)."""
        row = self._cur.fetchone()
        return row['id'] if row else None


class Connection:
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def cursor(self):
        return _Cursor(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, seq_of_params):
        return self.cursor().executemany(sql, seq_of_params)

    def executescript(self, script):
        """psycopg2 has no executescript, but a single cursor.execute() call
        with a semicolon-separated block works the same way in Postgres."""
        cur = self._conn.cursor()
        cur.execute(script)
        cur.close()

    def commit(self):
        self._conn.commit()

    def close(self):
        """Returns the connection to the pool rather than closing the
        socket. Always rolls back first — a no-op if the caller already
        committed, but it clears the implicit transaction any read-only
        caller left open (every statement starts one until commit/rollback)
        so the next borrower starts clean. If Neon has silently dropped
        this connection while it sat idle — its serverless compute can
        suspend independently of this pool's own lifetime — the rollback
        itself will raise, and the dead connection is discarded instead of
        being handed to the next request broken.
        """
        try:
            self._conn.rollback()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            try:
                _pool.putconn(self._conn, close=True)
            except Exception:
                pass
            return
        _pool.putconn(self._conn)


def connect():
    return Connection(_pool.getconn())
