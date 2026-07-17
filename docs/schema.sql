-- =============================================================================
-- User Payout Management System - Relational schema (SQLite dialect)
-- This mirrors app/database.py (the runtime source of truth) and documents the
-- tables, relationships, constraints and indexes.
--
-- Money is stored as INTEGER paise (1 rupee = 100 paise) to avoid float drift.
-- =============================================================================

-- Affiliate users who earn and withdraw.
CREATE TABLE users (
    id          TEXT PRIMARY KEY,                          -- e.g. 'john_doe'
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Brands a sale can belong to (brand_1, brand_2, brand_3, ...).
CREATE TABLE brands (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);

-- One wallet per user. The balance is the user's withdrawable amount.
-- It MAY go negative to represent a claw-back debt (an advance was withdrawn,
-- then the sale was rejected).
CREATE TABLE wallets (
    user_id                    TEXT PRIMARY KEY REFERENCES users(id),
    withdrawable_balance_paise INTEGER NOT NULL DEFAULT 0,
    updated_at                 TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A single affiliate sale. Enters as 'pending', later reconciled.
CREATE TABLE sales (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL REFERENCES users(id),
    brand_id      TEXT NOT NULL REFERENCES brands(id),
    status        TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected')),
    earning_paise INTEGER NOT NULL CHECK (earning_paise >= 0),
    reconciled_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sales_user_status ON sales(user_id, status);

-- Advance payouts (10% of a pending sale). UNIQUE(sale_id) is THE idempotency
-- guard: a sale can never receive a second advance, no matter how often the
-- advance job runs.
CREATE TABLE advance_payouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id      INTEGER NOT NULL UNIQUE REFERENCES sales(id),
    user_id      TEXT NOT NULL REFERENCES users(id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise >= 0),
    status       TEXT NOT NULL DEFAULT 'transferred',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per settled sale, recording the signed delta applied to the wallet:
--   approved -> earning - advance_paid ;  rejected -> -advance_paid
CREATE TABLE reconciliations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id            INTEGER NOT NULL UNIQUE REFERENCES sales(id),
    user_id            TEXT NOT NULL REFERENCES users(id),
    new_status         TEXT NOT NULL CHECK (new_status IN ('approved','rejected')),
    earning_paise      INTEGER NOT NULL,
    advance_paid_paise INTEGER NOT NULL,
    adjustment_paise   INTEGER NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Withdrawal (payout) requests. idempotency_key makes client retries safe.
CREATE TABLE withdrawals (
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
CREATE INDEX idx_withdrawals_user_created ON withdrawals(user_id, created_at);

-- Append-only audit log of EVERY wallet movement. Invariant:
--   SUM(ledger_entries.amount_paise) == wallets.withdrawable_balance_paise
CREATE TABLE ledger_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL REFERENCES users(id),
    entry_type          TEXT NOT NULL,   -- advance_credit | reconciliation_adjustment
                                         -- | withdrawal_debit | withdrawal_refund
    amount_paise        INTEGER NOT NULL,     -- signed: + credit, - debit
    balance_after_paise INTEGER NOT NULL,
    ref_type            TEXT,                 -- 'sale' | 'withdrawal'
    ref_id              TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_ledger_user ON ledger_entries(user_id, id);
