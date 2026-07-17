"""Composition root.

Wires repositories + services over a single ``Database`` and exposes the
account-level operations (create user/brand/sale, wallet & ledger reads, and the
payout summary). Keeping construction in one place makes the API layer thin and
lets tests build an isolated in-memory system in one line.
"""

import sqlite3

from .database import Database
from .constants import SaleStatus, WithdrawalStatus, ADVANCE_RATE
from .money import rupees_to_paise, advance_amount_paise
from .exceptions import NotFoundError, ConflictError
from .repositories import (
    UserRepository, BrandRepository, WalletRepository, SaleRepository,
    AdvancePayoutRepository, ReconciliationRepository, WithdrawalRepository,
    LedgerRepository,
)
from .services.wallet_service import WalletService
from .services.advance_service import AdvancePayoutService
from .services.reconciliation_service import ReconciliationService
from .services.withdrawal_service import WithdrawalService


class Container:
    def __init__(self, db: Database | None = None, now_fn=None):
        self.db = db or Database()

        # repositories
        self.users = UserRepository()
        self.brands = BrandRepository()
        self.wallets = WalletRepository()
        self.sales = SaleRepository()
        self.advances = AdvancePayoutRepository()
        self.reconciliations = ReconciliationRepository()
        self.withdrawals = WithdrawalRepository()
        self.ledger = LedgerRepository()

        # services
        self.wallet_service = WalletService(self.wallets, self.ledger)
        self.advance_service = AdvancePayoutService(
            self.db, self.sales, self.advances, self.wallet_service)
        self.reconciliation_service = ReconciliationService(
            self.db, self.sales, self.advances, self.reconciliations,
            self.wallet_service)
        wd_kwargs = {"now_fn": now_fn} if now_fn else {}
        self.withdrawal_service = WithdrawalService(
            self.db, self.withdrawals, self.wallet_service, **wd_kwargs)

    # ---- accounts / catalog -------------------------------------------------

    def create_user(self, user_id: str) -> dict:
        with self.db.transaction() as conn:
            if self.users.exists(conn, user_id):
                raise ConflictError(f"User '{user_id}' already exists.")
            self.users.create(conn, user_id)
        return {"id": user_id, "withdrawable_balance_paise": 0}

    def ensure_user(self, conn, user_id: str):
        if not self.users.exists(conn, user_id):
            self.users.create(conn, user_id)

    def create_brand(self, brand_id: str, name: str | None = None) -> dict:
        with self.db.transaction() as conn:
            self.brands.upsert(conn, brand_id, name)
        return {"id": brand_id, "name": name or brand_id}

    def list_brands(self) -> list[dict]:
        with self.db.read() as conn:
            return [dict(r) for r in self.brands.list(conn)]

    def create_sale(self, user_id: str, brand_id: str, earning) -> dict:
        earning_paise = rupees_to_paise(earning)
        if earning_paise < 0:
            raise ConflictError("earning cannot be negative.")
        with self.db.transaction() as conn:
            self.ensure_user(conn, user_id)          # convenience for demos
            if self.brands.get(conn, brand_id) is None:
                self.brands.upsert(conn, brand_id)   # auto-register brand
            sale_id = self.sales.create(conn, user_id, brand_id, earning_paise)
            row = self.sales.get(conn, sale_id)
            return dict(row)

    def get_sale(self, sale_id: int) -> dict:
        with self.db.read() as conn:
            row = self.sales.get(conn, sale_id)
            if row is None:
                raise NotFoundError(f"Sale {sale_id} not found.")
            return dict(row)

    def list_sales(self, user_id: str, status: str | None = None) -> list[dict]:
        with self.db.read() as conn:
            return [dict(r) for r in self.sales.list_for_user(conn, user_id, status)]

    # ---- wallet / ledger ----------------------------------------------------

    def get_wallet(self, user_id: str) -> dict:
        with self.db.read() as conn:
            wallet = self.wallets.get(conn, user_id)
            if wallet is None:
                raise NotFoundError(f"User '{user_id}' not found.")
            return dict(wallet)

    def get_ledger(self, user_id: str) -> list[dict]:
        with self.db.read() as conn:
            if not self.users.exists(conn, user_id):
                raise NotFoundError(f"User '{user_id}' not found.")
            return [dict(r) for r in self.ledger.list_for_user(conn, user_id)]

    # ---- payout summary -----------------------------------------------------

    def payout_summary(self, user_id: str) -> dict:
        """A single snapshot of a user's payout position."""
        with self.db.read() as conn:
            if not self.users.exists(conn, user_id):
                raise NotFoundError(f"User '{user_id}' not found.")

            sales = self.sales.list_for_user(conn, user_id)
            pending = [s for s in sales if s["status"] == SaleStatus.PENDING]
            approved = [s for s in sales if s["status"] == SaleStatus.APPROVED]
            rejected = [s for s in sales if s["status"] == SaleStatus.REJECTED]

            pending_earnings = sum(s["earning_paise"] for s in pending)
            approved_earnings = sum(s["earning_paise"] for s in approved)

            # Advance still owed on pending sales that have none yet.
            eligible = self.sales.pending_without_advance(conn, user_id)
            advance_due = sum(advance_amount_paise(s["earning_paise"]) for s in eligible)
            advance_paid = self.advances.total_for_user(conn, user_id)

            balance = self.wallet_service.get_balance(conn, user_id)

            return {
                "user_id": user_id,
                "advance_rate": float(ADVANCE_RATE),
                "counts": {
                    "pending": len(pending),
                    "approved": len(approved),
                    "rejected": len(rejected),
                },
                "pending_earnings_paise": pending_earnings,
                "approved_earnings_paise": approved_earnings,
                "eligible_advance_now_paise": advance_due,
                "advance_paid_to_date_paise": advance_paid,
                "withdrawable_balance_paise": balance,
            }
