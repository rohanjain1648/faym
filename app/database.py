"""SQLite persistence with explicit, serialized transactions.

Design notes
------------
* A single shared connection guarded by an ``RLock`` serializes all access.
  For a demo/assignment this is the simplest way to get correct, race-free
  balance updates and idempotency without a heavier engine. In production this
  layer would be swapped for a connection pool against Postgres and the same
  transaction boundaries would hold.
* Writes run inside ``transaction()`` (``BEGIN IMMEDIATE`` -> commit/rollback)
  so a whole business operation (e.g. debit wallet + record withdrawal + write
  ledger) is atomic.
* ``PRAGMA foreign_keys = ON`` enforces referential integrity.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "payouts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS brands (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);

-- One wallet per user. Balance may go negative to represent a claw-back debt
-- (an advance was withdrawn, then the sale was rejected).
CREATE TABLE IF NOT EXISTS wallets (
    user_id                   TEXT PRIMARY KEY REFERENCES users(id),
    withdrawable_balance_paise INTEGER NOT NULL DEFAULT 0,
    updated_at                TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sales (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL REFERENCES users(id),
    brand_id      TEXT NOT NULL REFERENCES brands(id),
    status        TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected')),
    earning_paise INTEGER NOT NULL CHECK (earning_paise >= 0),
    reconciled_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sales_user_status ON sales(user_id, status);

-- Advance payouts. UNIQUE(sale_id) is the idempotency guard: a sale can never
-- receive a second advance no matter how many times the job runs.
CREATE TABLE IF NOT EXISTS advance_payouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id      INTEGER NOT NULL UNIQUE REFERENCES sales(id),
    user_id      TEXT NOT NULL REFERENCES users(id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise >= 0),
    status       TEXT NOT NULL DEFAULT 'transferred',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Records the settlement delta applied to the wallet for each reconciled sale.
CREATE TABLE IF NOT EXISTS reconciliations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id           INTEGER NOT NULL UNIQUE REFERENCES sales(id),
    user_id           TEXT NOT NULL REFERENCES users(id),
    new_status        TEXT NOT NULL CHECK (new_status IN ('approved','rejected')),
    earning_paise     INTEGER NOT NULL,
    advance_paid_paise INTEGER NOT NULL,
    adjustment_paise  INTEGER NOT NULL,   -- signed delta applied to the wallet
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL REFERENCES users(id),
    amount_paise     INTEGER NOT NULL CHECK (amount_paise > 0),
    status           TEXT NOT NULL DEFAULT 'initiated'
                       CHECK (status IN ('initiated','processing','completed',
                                         'failed','cancelled','rejected')),
    idempotency_key  TEXT UNIQUE,
    failure_reason   TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user_created ON withdrawals(user_id, created_at);

-- Append-only audit log of every wallet movement.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            TEXT NOT NULL REFERENCES users(id),
    entry_type         TEXT NOT NULL,
    amount_paise       INTEGER NOT NULL,   -- signed: + credit, - debit
    balance_after_paise INTEGER NOT NULL,
    ref_type           TEXT,               -- 'sale' | 'withdrawal'
    ref_id             TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger_entries(user_id, id);
"""


class Database:
    """Thin wrapper over a shared, lock-serialized SQLite connection."""

    def __init__(self, path=DEFAULT_DB_PATH):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """Atomic write scope. Yields the connection; commits or rolls back."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @contextmanager
    def read(self):
        """Read scope. Serialized with writes via the same lock."""
        with self._lock:
            yield self._conn

    def close(self):
        with self._lock:
            self._conn.close()
