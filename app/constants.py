"""Domain constants: statuses, ledger entry types, and business-rule parameters."""

from decimal import Decimal


class SaleStatus:
    """Lifecycle of a sale."""

    PENDING = "pending"     # Product purchased, not yet settled.
    APPROVED = "approved"   # Delivered and return window closed -> user earns it.
    REJECTED = "rejected"   # Returned/cancelled -> user earns nothing.

    ALL = {PENDING, APPROVED, REJECTED}
    RECONCILED = {APPROVED, REJECTED}


class WithdrawalStatus:
    """Lifecycle of a payout withdrawal to the user."""

    INITIATED = "initiated"    # Requested; balance already debited.
    PROCESSING = "processing"  # Sent to the payment provider.
    COMPLETED = "completed"    # Money reached the user.
    FAILED = "failed"          # Provider failed the transfer.
    CANCELLED = "cancelled"    # Transfer cancelled.
    REJECTED = "rejected"      # Transfer rejected (e.g. bad account).

    ALL = {INITIATED, PROCESSING, COMPLETED, FAILED, CANCELLED, REJECTED}
    # States in which the money is still "out" (counts against the 24h window).
    ACTIVE = {INITIATED, PROCESSING, COMPLETED}
    # Terminal failure states that trigger a refund back to the wallet.
    RECOVERABLE = {FAILED, CANCELLED, REJECTED}
    TERMINAL = {COMPLETED, FAILED, CANCELLED, REJECTED}


class LedgerType:
    """Reason codes for every movement in the wallet ledger."""

    ADVANCE_CREDIT = "advance_credit"                # +10% advance on a pending sale.
    RECONCILIATION = "reconciliation_adjustment"     # +/- delta when a sale is settled.
    WITHDRAWAL_DEBIT = "withdrawal_debit"            # - user withdraws funds.
    WITHDRAWAL_REFUND = "withdrawal_refund"          # + failed payout returned to wallet.


# ---- Business-rule parameters -------------------------------------------------

# Advance payout = 10% of a pending sale's earnings.
ADVANCE_RATE = Decimal("0.10")

# A user may make only one payout withdrawal every 24 hours.
WITHDRAWAL_COOLDOWN_HOURS = 24
