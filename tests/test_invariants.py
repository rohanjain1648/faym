"""Cross-cutting invariants that must hold after any sequence of operations."""

from app.money import rupees_to_paise


def _ledger_sum(app, user):
    return sum(e["amount_paise"] for e in app.get_ledger(user))


def test_ledger_sum_equals_wallet_balance(app, user, clock):
    ids = [app.create_sale(user, "brand_1", 40)["id"] for _ in range(3)]
    app.advance_service.run(user)
    app.reconciliation_service.reconcile(ids[0], "rejected")
    app.reconciliation_service.reconcile(ids[1], "approved")

    wd = app.withdrawal_service.request(user, rupees_to_paise(20))
    app.withdrawal_service.update_status(wd["id"], "failed")

    balance = app.get_wallet(user)["withdrawable_balance_paise"]
    assert _ledger_sum(app, user) == balance


def test_balance_can_go_negative_on_clawback_after_withdrawal(app, user):
    # Advance is paid, user withdraws it, then the sale is rejected -> debt.
    sale = app.create_sale(user, "brand_1", 50)     # advance ₹5
    app.advance_service.run(user)
    app.withdrawal_service.request(user, rupees_to_paise(5))   # withdraw the advance
    assert app.get_wallet(user)["withdrawable_balance_paise"] == 0

    app.reconciliation_service.reconcile(sale["id"], "rejected")  # claw back ₹5
    # The user now owes ₹5, represented as a negative (debt) balance.
    assert app.get_wallet(user)["withdrawable_balance_paise"] == rupees_to_paise(-5)
    assert _ledger_sum(app, user) == rupees_to_paise(-5)
