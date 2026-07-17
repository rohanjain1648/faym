"""Business rule #2: final payout calculation on reconciliation."""

import pytest

from app.money import rupees_to_paise
from app.exceptions import ConflictError, ValidationError, NotFoundError


def test_approved_sale_credits_earning_minus_advance(app, user):
    # Case 1 from the spec: earning ₹30, advance ₹3 -> +₹27 on approval.
    sale = app.create_sale(user, "brand_1", 30)
    app.advance_service.run(user)

    result = app.reconciliation_service.reconcile(sale["id"], "approved")

    assert result["adjustment_paise"] == rupees_to_paise(27)
    # advance 3 + settlement 27 = 30 (the full earning).
    assert app.get_wallet(user)["withdrawable_balance_paise"] == rupees_to_paise(30)


def test_rejected_sale_claws_back_advance(app, user):
    # Case 2 from the spec: earning ₹50, advance ₹5 -> adjustment -₹5 on reject.
    sale = app.create_sale(user, "brand_1", 50)
    app.advance_service.run(user)

    result = app.reconciliation_service.reconcile(sale["id"], "rejected")

    assert result["adjustment_paise"] == rupees_to_paise(-5)
    # advance 5 - clawback 5 = 0 earned.
    assert app.get_wallet(user)["withdrawable_balance_paise"] == 0


def test_assignment_worked_example_totals_68(app, user):
    ids = [app.create_sale(user, "brand_1", 40)["id"] for _ in range(3)]
    advance = app.advance_service.run(user)
    assert advance["total_advance_paise"] == rupees_to_paise(12)

    app.reconciliation_service.reconcile(ids[0], "rejected")
    app.reconciliation_service.reconcile(ids[1], "approved")
    app.reconciliation_service.reconcile(ids[2], "approved")

    balance = app.get_wallet(user)["withdrawable_balance_paise"]
    assert balance == rupees_to_paise(80)                       # total approved earnings
    assert balance - advance["total_advance_paise"] == rupees_to_paise(68)  # final payout


def test_reconcile_without_advance_still_works(app, user):
    # No advance job was run; approving should credit the full earning.
    sale = app.create_sale(user, "brand_1", 40)
    result = app.reconciliation_service.reconcile(sale["id"], "approved")
    assert result["advance_paid_paise"] == 0
    assert result["adjustment_paise"] == rupees_to_paise(40)


def test_cannot_reconcile_twice(app, user):
    sale = app.create_sale(user, "brand_1", 40)
    app.reconciliation_service.reconcile(sale["id"], "approved")
    with pytest.raises(ConflictError):
        app.reconciliation_service.reconcile(sale["id"], "rejected")


def test_invalid_status_rejected(app, user):
    sale = app.create_sale(user, "brand_1", 40)
    with pytest.raises(ValidationError):
        app.reconciliation_service.reconcile(sale["id"], "pending")


def test_reconcile_missing_sale(app, user):
    with pytest.raises(NotFoundError):
        app.reconciliation_service.reconcile(9999, "approved")


def test_batch_reconcile_isolates_errors(app, user):
    ok = app.create_sale(user, "brand_1", 40)["id"]
    result = app.reconciliation_service.reconcile_batch([
        {"sale_id": ok, "status": "approved"},
        {"sale_id": 9999, "status": "approved"},   # missing -> error, not fatal
    ])
    assert len(result["reconciled"]) == 1
    assert len(result["errors"]) == 1
