"""Advance payout job.

Business rule #1: every *pending* sale is eligible for an advance of 10% of its
earnings, and a sale must **never** receive a second advance even if the job
runs many times.

Idempotency is enforced at two levels:
* The work-list query (`pending_without_advance`) only returns sales that have
  no advance row yet.
* The `UNIQUE(sale_id)` constraint on `advance_payouts` is the hard backstop —
  even under a concurrent double-run, the second insert fails and is skipped.
"""

import sqlite3

from ..money import advance_amount_paise
from ..constants import LedgerType
from ..repositories import SaleRepository, AdvancePayoutRepository
from .wallet_service import WalletService


class AdvancePayoutService:
    def __init__(self, db, sales: SaleRepository,
                 advances: AdvancePayoutRepository, wallet: WalletService):
        self._db = db
        self._sales = sales
        self._advances = advances
        self._wallet = wallet

    def run(self, user_id: str | None = None) -> dict:
        """Pay advances for all eligible pending sales.

        Safe to run repeatedly (cron/on-demand). Returns a summary.
        """
        with self._db.transaction() as conn:
            eligible = self._sales.pending_without_advance(conn, user_id)
            paid = []
            total = 0
            for sale in eligible:
                amount = advance_amount_paise(sale["earning_paise"])
                try:
                    self._advances.create(conn, sale["id"], sale["user_id"], amount)
                except sqlite3.IntegrityError:
                    # UNIQUE(sale_id) tripped: another run already paid it. Skip.
                    continue
                self._wallet.apply(
                    conn,
                    user_id=sale["user_id"],
                    amount_paise=amount,
                    entry_type=LedgerType.ADVANCE_CREDIT,
                    ref_type="sale",
                    ref_id=sale["id"],
                )
                paid.append({"sale_id": sale["id"], "amount_paise": amount})
                total += amount

            return {
                "sales_paid": len(paid),
                "total_advance_paise": total,
                "details": paid,
            }
