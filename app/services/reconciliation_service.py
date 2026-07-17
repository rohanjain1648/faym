"""Reconciliation: settle a pending sale to approved/rejected.

Business rule #2 — Final Payout Calculation. When an admin reconciles a sale we
apply a signed *delta* to the wallet so that, across the sale's whole life, the
user ends up with exactly what they earned:

    Approved sale:  delta = earning - advance_already_paid
                    (they keep the advance, get the rest)
    Rejected sale:  delta = -advance_already_paid
                    (claw back the advance they were not entitled to)

Because the advance already credited `advance_paid`, the running balance after
settling equals:  approved-earnings + 0  =  total legitimately earned.

Worked example (assignment): 3x rejected/approved/approved @ ₹40, advance ₹4 each
    rejected  -> -4
    approved  -> +36
    approved  -> +36
    reconciliation delta total = 68; advance already credited 12; balance = 80
    (= the two approved ₹40 sales). Final payout beyond the advance = ₹68.
"""

from ..constants import SaleStatus, LedgerType
from ..exceptions import NotFoundError, ConflictError, ValidationError
from ..repositories import (
    SaleRepository, AdvancePayoutRepository, ReconciliationRepository,
)
from .wallet_service import WalletService


class ReconciliationService:
    def __init__(self, db, sales: SaleRepository,
                 advances: AdvancePayoutRepository,
                 reconciliations: ReconciliationRepository,
                 wallet: WalletService):
        self._db = db
        self._sales = sales
        self._advances = advances
        self._reconciliations = reconciliations
        self._wallet = wallet

    def reconcile(self, sale_id: int, new_status: str) -> dict:
        if new_status not in (SaleStatus.APPROVED, SaleStatus.REJECTED):
            raise ValidationError(
                "new_status must be 'approved' or 'rejected'."
            )

        with self._db.transaction() as conn:
            sale = self._sales.get(conn, sale_id)
            if sale is None:
                raise NotFoundError(f"Sale {sale_id} not found.")
            if sale["status"] != SaleStatus.PENDING:
                raise ConflictError(
                    f"Sale {sale_id} is already '{sale['status']}' and cannot be "
                    "reconciled again."
                )

            advance = self._advances.get_for_sale(conn, sale_id)
            advance_paid = advance["amount_paise"] if advance else 0
            earning = sale["earning_paise"]

            if new_status == SaleStatus.APPROVED:
                adjustment = earning - advance_paid
            else:  # rejected
                adjustment = -advance_paid

            self._sales.mark_reconciled(conn, sale_id, new_status)
            self._reconciliations.create(
                conn,
                sale_id=sale_id,
                user_id=sale["user_id"],
                new_status=new_status,
                earning_paise=earning,
                advance_paid_paise=advance_paid,
                adjustment_paise=adjustment,
            )
            new_balance = self._wallet.apply(
                conn,
                user_id=sale["user_id"],
                amount_paise=adjustment,
                entry_type=LedgerType.RECONCILIATION,
                ref_type="sale",
                ref_id=sale_id,
            )

            return {
                "sale_id": sale_id,
                "user_id": sale["user_id"],
                "new_status": new_status,
                "earning_paise": earning,
                "advance_paid_paise": advance_paid,
                "adjustment_paise": adjustment,
                "wallet_balance_paise": new_balance,
            }

    def reconcile_batch(self, items: list[dict]) -> dict:
        """Reconcile many sales. Each item: {"sale_id", "status"}.

        Processed independently so one bad item does not roll back the rest;
        per-item results/errors are returned.
        """
        results, errors = [], []
        for item in items:
            try:
                results.append(self.reconcile(item["sale_id"], item["status"]))
            except Exception as exc:  # noqa: BLE001 - reported per item
                errors.append({"sale_id": item.get("sale_id"), "error": str(exc)})
        return {"reconciled": results, "errors": errors}
