"""Business rule #3: withdrawal balance guards and the 24h cooldown."""

import pytest

from app.money import rupees_to_paise
from app.exceptions import (
    InsufficientBalanceError, WithdrawalCooldownError, ValidationError,
)


def _fund(app, user, rupees):
    """Give the user a withdrawable balance by approving a no-advance sale."""
    sale = app.create_sale(user, "brand_1", rupees)
    app.reconciliation_service.reconcile(sale["id"], "approved")


def test_withdrawal_debits_balance(app, user):
    _fund(app, user, 80)
    wd = app.withdrawal_service.request(user, rupees_to_paise(80))
    assert wd["status"] == "initiated"
    assert wd["wallet_balance_paise"] == 0


def test_cannot_withdraw_more_than_balance(app, user):
    _fund(app, user, 50)
    with pytest.raises(InsufficientBalanceError):
        app.withdrawal_service.request(user, rupees_to_paise(60))


def test_zero_or_negative_amount_rejected(app, user):
    _fund(app, user, 50)
    with pytest.raises(ValidationError):
        app.withdrawal_service.request(user, 0)


def test_only_one_withdrawal_per_24h(app, user, clock):
    _fund(app, user, 100)
    app.withdrawal_service.request(user, rupees_to_paise(30))

    # Second attempt an hour later is blocked.
    clock.advance(hours=1)
    with pytest.raises(WithdrawalCooldownError) as exc:
        app.withdrawal_service.request(user, rupees_to_paise(30))
    assert exc.value.retry_after_seconds > 0


def test_withdrawal_allowed_after_24h(app, user, clock):
    _fund(app, user, 100)
    app.withdrawal_service.request(user, rupees_to_paise(30))

    clock.advance(hours=24, minutes=1)
    wd = app.withdrawal_service.request(user, rupees_to_paise(30))
    assert wd["status"] == "initiated"


def test_idempotency_key_prevents_double_debit(app, user):
    _fund(app, user, 100)
    first = app.withdrawal_service.request(user, rupees_to_paise(40), "req-123")
    second = app.withdrawal_service.request(user, rupees_to_paise(40), "req-123")

    assert first["id"] == second["id"]
    # Only one debit happened.
    assert app.get_wallet(user)["withdrawable_balance_paise"] == rupees_to_paise(60)
