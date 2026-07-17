"""Question 2: failed payout recovery."""

import pytest

from app.money import rupees_to_paise
from app.exceptions import ConflictError


def _fund(app, user, rupees):
    sale = app.create_sale(user, "brand_1", rupees)
    app.reconciliation_service.reconcile(sale["id"], "approved")


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "rejected"])
def test_failed_payout_is_refunded(app, user, terminal):
    _fund(app, user, 80)
    wd = app.withdrawal_service.request(user, rupees_to_paise(80))
    assert app.get_wallet(user)["withdrawable_balance_paise"] == 0

    result = app.withdrawal_service.update_status(wd["id"], terminal, "reason")

    assert result["refunded"] is True
    assert result["status"] == terminal
    assert app.get_wallet(user)["withdrawable_balance_paise"] == rupees_to_paise(80)


def test_completed_payout_is_not_refunded(app, user):
    _fund(app, user, 80)
    wd = app.withdrawal_service.request(user, rupees_to_paise(80))

    result = app.withdrawal_service.update_status(wd["id"], "completed")

    assert result["refunded"] is False
    assert app.get_wallet(user)["withdrawable_balance_paise"] == 0


def test_user_can_rewithdraw_after_failure_without_cooldown(app, user, clock):
    _fund(app, user, 80)
    wd = app.withdrawal_service.request(user, rupees_to_paise(80))
    app.withdrawal_service.update_status(wd["id"], "failed", "bank timeout")

    # Immediately (same instant) a new withdrawal is allowed: the failed one
    # no longer counts against the 24h window.
    retry = app.withdrawal_service.request(user, rupees_to_paise(80))
    assert retry["status"] == "initiated"
    assert retry["id"] != wd["id"]


def test_cannot_change_a_completed_withdrawal(app, user):
    _fund(app, user, 80)
    wd = app.withdrawal_service.request(user, rupees_to_paise(80))
    app.withdrawal_service.update_status(wd["id"], "completed")
    with pytest.raises(ConflictError):
        app.withdrawal_service.update_status(wd["id"], "failed")


def test_double_failure_refunds_only_once(app, user):
    _fund(app, user, 80)
    wd = app.withdrawal_service.request(user, rupees_to_paise(80))
    app.withdrawal_service.update_status(wd["id"], "failed")
    # A second terminal transition is rejected -> no second refund.
    with pytest.raises(ConflictError):
        app.withdrawal_service.update_status(wd["id"], "cancelled")
    assert app.get_wallet(user)["withdrawable_balance_paise"] == rupees_to_paise(80)
