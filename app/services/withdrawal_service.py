"""Withdrawal service.

Business rule #3 — a user may make only one payout withdrawal every 24 hours.
Question 2 — failed/cancelled/rejected payouts are credited back so the user can
withdraw again.

Guards on request:
* amount must be > 0 and <= withdrawable balance,
* no other *active* (initiated/processing/completed) withdrawal inside 24h,
* optional idempotency key makes retries safe (returns the existing row).

On a terminal failure we refund exactly once and it stops counting against the
24h window, so the user can immediately re-initiate.
"""

from datetime import datetime, timedelta, timezone

from ..constants import WithdrawalStatus, LedgerType, WITHDRAWAL_COOLDOWN_HOURS
from ..exceptions import (
    NotFoundError, ConflictError, ValidationError,
    InsufficientBalanceError, WithdrawalCooldownError,
)
from ..repositories import WithdrawalRepository
from .wallet_service import WalletService

# SQLite stores our timestamps as UTC 'YYYY-MM-DD HH:MM:SS' (via datetime('now')).
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc)


class WithdrawalService:
    def __init__(self, db, withdrawals: WithdrawalRepository, wallet: WalletService,
                 now_fn=lambda: datetime.now(timezone.utc)):
        self._db = db
        self._withdrawals = withdrawals
        self._wallet = wallet
        self._now = now_fn  # injectable clock for testing the 24h rule

    # ---- request ------------------------------------------------------------

    def request(self, user_id: str, amount_paise: int,
                idempotency_key: str | None = None) -> dict:
        if amount_paise <= 0:
            raise ValidationError("Withdrawal amount must be positive.")

        with self._db.transaction() as conn:
            if idempotency_key:
                existing = self._withdrawals.get_by_idempotency_key(
                    conn, idempotency_key)
                if existing:
                    # Safe retry: return the original result, no double debit.
                    return self._to_dict(existing, self._wallet.get_balance(
                        conn, user_id))

            self._enforce_cooldown(conn, user_id)

            balance = self._wallet.get_balance(conn, user_id)
            if amount_paise > balance:
                raise InsufficientBalanceError(
                    f"Requested {amount_paise} paise but withdrawable balance is "
                    f"{balance} paise."
                )

            withdrawal_id = self._withdrawals.create(
                conn, user_id, amount_paise, idempotency_key,
                created_at=self._now().strftime(_TS_FMT))
            new_balance = self._wallet.apply(
                conn,
                user_id=user_id,
                amount_paise=-amount_paise,
                entry_type=LedgerType.WITHDRAWAL_DEBIT,
                ref_type="withdrawal",
                ref_id=withdrawal_id,
            )
            row = self._withdrawals.get(conn, withdrawal_id)
            return self._to_dict(row, new_balance)

    def _enforce_cooldown(self, conn, user_id: str):
        latest = self._withdrawals.latest_active(conn, user_id)
        if latest is None:
            return
        last_at = _parse_ts(latest["created_at"])
        window = timedelta(hours=WITHDRAWAL_COOLDOWN_HOURS)
        elapsed = self._now() - last_at
        if elapsed < window:
            retry_after = int((window - elapsed).total_seconds())
            raise WithdrawalCooldownError(
                f"Only one withdrawal allowed per {WITHDRAWAL_COOLDOWN_HOURS}h. "
                f"Try again in ~{retry_after // 3600}h "
                f"{(retry_after % 3600) // 60}m.",
                retry_after_seconds=retry_after,
            )

    # ---- provider status callback ------------------------------------------

    def update_status(self, withdrawal_id: int, new_status: str,
                      failure_reason: str | None = None) -> dict:
        """Advance a withdrawal to a provider-reported status.

        Terminal failures (failed/cancelled/rejected) refund the wallet exactly
        once so the user can withdraw the amount again.
        """
        if new_status not in WithdrawalStatus.ALL:
            raise ValidationError(f"Unknown withdrawal status '{new_status}'.")

        with self._db.transaction() as conn:
            wd = self._withdrawals.get(conn, withdrawal_id)
            if wd is None:
                raise NotFoundError(f"Withdrawal {withdrawal_id} not found.")

            current = wd["status"]
            if current in WithdrawalStatus.TERMINAL:
                raise ConflictError(
                    f"Withdrawal {withdrawal_id} is already terminal "
                    f"('{current}') and cannot change."
                )

            self._withdrawals.update_status(
                conn, withdrawal_id, new_status, failure_reason)

            refunded = False
            balance = self._wallet.get_balance(conn, user_id=wd["user_id"])
            if new_status in WithdrawalStatus.RECOVERABLE:
                balance = self._wallet.apply(
                    conn,
                    user_id=wd["user_id"],
                    amount_paise=wd["amount_paise"],  # credit back
                    entry_type=LedgerType.WITHDRAWAL_REFUND,
                    ref_type="withdrawal",
                    ref_id=withdrawal_id,
                )
                refunded = True

            row = self._withdrawals.get(conn, withdrawal_id)
            result = self._to_dict(row, balance)
            result["refunded"] = refunded
            return result

    # ---- helpers ------------------------------------------------------------

    def list_for_user(self, user_id: str) -> list[dict]:
        with self._db.read() as conn:
            rows = self._withdrawals.list_for_user(conn, user_id)
            return [self._to_dict(r) for r in rows]

    @staticmethod
    def _to_dict(row, wallet_balance_paise: int | None = None) -> dict:
        data = {
            "id": row["id"],
            "user_id": row["user_id"],
            "amount_paise": row["amount_paise"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "failure_reason": row["failure_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if wallet_balance_paise is not None:
            data["wallet_balance_paise"] = wallet_balance_paise
        return data
