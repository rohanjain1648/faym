"""Wallet service: the single choke-point for balance changes.

Every credit/debit updates the wallet balance and writes a ledger entry in the
same transaction, so the ledger is always a faithful, append-only history and
``SUM(ledger.amount) == wallet.balance`` holds at all times.

Callers must already be inside a ``db.transaction()`` and pass the ``conn``.
"""

from ..repositories import WalletRepository, LedgerRepository


class WalletService:
    def __init__(self, wallets: WalletRepository, ledger: LedgerRepository):
        self._wallets = wallets
        self._ledger = ledger

    def get_balance(self, conn, user_id: str) -> int:
        wallet = self._wallets.get(conn, user_id)
        return wallet["withdrawable_balance_paise"] if wallet else 0

    def apply(self, conn, *, user_id: str, amount_paise: int, entry_type: str,
              ref_type: str | None = None, ref_id=None) -> int:
        """Apply a signed movement (+credit / -debit) atomically.

        Returns the new balance. The wallet balance is allowed to go negative to
        represent a claw-back debt (e.g. an advance was withdrawn before the
        sale was rejected); withdrawal guards prevent spending money that isn't
        there.
        """
        current = self.get_balance(conn, user_id)
        new_balance = current + amount_paise
        self._wallets.set_balance(conn, user_id, new_balance)
        self._ledger.add(
            conn,
            user_id=user_id,
            entry_type=entry_type,
            amount_paise=amount_paise,
            balance_after_paise=new_balance,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        return new_balance
