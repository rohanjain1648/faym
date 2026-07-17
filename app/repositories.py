"""Data-access layer.

Repositories are stateless query helpers: every method takes the active
``conn`` (obtained from ``Database.transaction()`` or ``Database.read()``) so
that a single business operation can touch several tables inside one atomic
transaction. Rows are returned as ``sqlite3.Row`` (dict-like).
"""

from .constants import SaleStatus, WithdrawalStatus


class UserRepository:
    def create(self, conn, user_id: str):
        conn.execute("INSERT INTO users (id) VALUES (?)", (user_id,))
        conn.execute("INSERT INTO wallets (user_id) VALUES (?)", (user_id,))

    def get(self, conn, user_id: str):
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def exists(self, conn, user_id: str) -> bool:
        return self.get(conn, user_id) is not None


class BrandRepository:
    def upsert(self, conn, brand_id: str, name: str | None = None):
        conn.execute(
            "INSERT INTO brands (id, name) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            (brand_id, name or brand_id),
        )

    def get(self, conn, brand_id: str):
        return conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()

    def list(self, conn):
        return conn.execute("SELECT * FROM brands ORDER BY id").fetchall()


class WalletRepository:
    def get(self, conn, user_id: str):
        return conn.execute(
            "SELECT * FROM wallets WHERE user_id = ?", (user_id,)
        ).fetchone()

    def set_balance(self, conn, user_id: str, balance_paise: int):
        conn.execute(
            "UPDATE wallets SET withdrawable_balance_paise = ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (balance_paise, user_id),
        )


class SaleRepository:
    def create(self, conn, user_id: str, brand_id: str, earning_paise: int,
               status: str = SaleStatus.PENDING) -> int:
        cur = conn.execute(
            "INSERT INTO sales (user_id, brand_id, status, earning_paise) "
            "VALUES (?, ?, ?, ?)",
            (user_id, brand_id, status, earning_paise),
        )
        return cur.lastrowid

    def get(self, conn, sale_id: int):
        return conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()

    def list_for_user(self, conn, user_id: str, status: str | None = None):
        if status:
            return conn.execute(
                "SELECT * FROM sales WHERE user_id = ? AND status = ? ORDER BY id",
                (user_id, status),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM sales WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()

    def pending_without_advance(self, conn, user_id: str | None = None):
        """Pending sales that have no advance payout yet (job work-list)."""
        sql = (
            "SELECT s.* FROM sales s "
            "LEFT JOIN advance_payouts a ON a.sale_id = s.id "
            "WHERE s.status = 'pending' AND a.id IS NULL"
        )
        params: tuple = ()
        if user_id:
            sql += " AND s.user_id = ?"
            params = (user_id,)
        sql += " ORDER BY s.id"
        return conn.execute(sql, params).fetchall()

    def mark_reconciled(self, conn, sale_id: int, new_status: str):
        conn.execute(
            "UPDATE sales SET status = ?, reconciled_at = datetime('now') WHERE id = ?",
            (new_status, sale_id),
        )


class AdvancePayoutRepository:
    def create(self, conn, sale_id: int, user_id: str, amount_paise: int) -> int:
        """Insert an advance. Relies on UNIQUE(sale_id) for idempotency."""
        cur = conn.execute(
            "INSERT INTO advance_payouts (sale_id, user_id, amount_paise) "
            "VALUES (?, ?, ?)",
            (sale_id, user_id, amount_paise),
        )
        return cur.lastrowid

    def get_for_sale(self, conn, sale_id: int):
        return conn.execute(
            "SELECT * FROM advance_payouts WHERE sale_id = ?", (sale_id,)
        ).fetchone()

    def total_for_user(self, conn, user_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS total "
            "FROM advance_payouts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["total"]


class ReconciliationRepository:
    def create(self, conn, *, sale_id, user_id, new_status, earning_paise,
               advance_paid_paise, adjustment_paise):
        conn.execute(
            "INSERT INTO reconciliations "
            "(sale_id, user_id, new_status, earning_paise, advance_paid_paise, "
            " adjustment_paise) VALUES (?, ?, ?, ?, ?, ?)",
            (sale_id, user_id, new_status, earning_paise, advance_paid_paise,
             adjustment_paise),
        )

    def get_for_sale(self, conn, sale_id: int):
        return conn.execute(
            "SELECT * FROM reconciliations WHERE sale_id = ?", (sale_id,)
        ).fetchone()


class WithdrawalRepository:
    def create(self, conn, user_id: str, amount_paise: int,
               idempotency_key: str | None = None,
               created_at: str | None = None) -> int:
        # created_at is supplied by the service (from its injectable clock) so
        # the 24h cooldown compares timestamps against the same clock.
        if created_at is None:
            cur = conn.execute(
                "INSERT INTO withdrawals (user_id, amount_paise, status, idempotency_key) "
                "VALUES (?, ?, ?, ?)",
                (user_id, amount_paise, WithdrawalStatus.INITIATED, idempotency_key),
            )
        else:
            cur = conn.execute(
                "INSERT INTO withdrawals "
                "(user_id, amount_paise, status, idempotency_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, amount_paise, WithdrawalStatus.INITIATED, idempotency_key,
                 created_at, created_at),
            )
        return cur.lastrowid

    def get(self, conn, withdrawal_id: int):
        return conn.execute(
            "SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,)
        ).fetchone()

    def get_by_idempotency_key(self, conn, key: str):
        return conn.execute(
            "SELECT * FROM withdrawals WHERE idempotency_key = ?", (key,)
        ).fetchone()

    def update_status(self, conn, withdrawal_id: int, status: str,
                      failure_reason: str | None = None):
        conn.execute(
            "UPDATE withdrawals SET status = ?, failure_reason = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (status, failure_reason, withdrawal_id),
        )

    def latest_active(self, conn, user_id: str):
        """Most recent withdrawal that still counts against the 24h window."""
        placeholders = ",".join("?" for _ in WithdrawalStatus.ACTIVE)
        return conn.execute(
            f"SELECT * FROM withdrawals WHERE user_id = ? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id, *WithdrawalStatus.ACTIVE),
        ).fetchone()

    def list_for_user(self, conn, user_id: str):
        return conn.execute(
            "SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()


class LedgerRepository:
    def add(self, conn, *, user_id, entry_type, amount_paise,
            balance_after_paise, ref_type=None, ref_id=None):
        conn.execute(
            "INSERT INTO ledger_entries "
            "(user_id, entry_type, amount_paise, balance_after_paise, ref_type, ref_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, entry_type, amount_paise, balance_after_paise,
             ref_type, str(ref_id) if ref_id is not None else None),
        )

    def list_for_user(self, conn, user_id: str):
        return conn.execute(
            "SELECT * FROM ledger_entries WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
