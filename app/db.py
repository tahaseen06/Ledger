import os
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
EXTERNAL_NUMBER = "EXTERNAL"


def db_path():
    return os.environ.get("LEDGERLINE_DB", "ledgerline.db")


def connect(path=None):
    """One connection per request. Autocommit mode: transactions are explicit (BEGIN IMMEDIATE)."""
    conn = sqlite3.connect(path or db_path(), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(path=None):
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA.read_text())
        row = conn.execute("SELECT id FROM accounts WHERE number=?", (EXTERNAL_NUMBER,)).fetchone()
        if not row:
            from .ledger import now
            # The external account belongs to no user; it represents "the outside world".
            conn.execute(
                "INSERT INTO accounts(number,name,kind,owner_id,created_at) VALUES(?,?,?,NULL,?)",
                (EXTERNAL_NUMBER, "External world (counterparty of deposits/withdrawals)", "external", now()),
            )
    finally:
        conn.close()


def external_id(conn):
    return conn.execute("SELECT id FROM accounts WHERE kind='external'").fetchone()["id"]
