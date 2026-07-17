"""Business rule #1: advance payout (10%) and its idempotency."""

from app.money import rupees_to_paise


def test_advance_is_ten_percent_of_pending_earnings(app, user):
    app.create_sale(user, "brand_1", 40)
    result = app.advance_service.run(user)

    assert result["sales_paid"] == 1
    assert result["total_advance_paise"] == rupees_to_paise(4)  # 10% of 40
    assert app.get_wallet(user)["withdrawable_balance_paise"] == rupees_to_paise(4)


def test_advance_across_multiple_sales(app, user):
    for _ in range(3):
        app.create_sale(user, "brand_1", 40)  # ₹120 total pending

    result = app.advance_service.run(user)

    assert result["sales_paid"] == 3
    assert result["total_advance_paise"] == rupees_to_paise(12)  # 10% of 120


def test_advance_job_is_idempotent(app, user):
    app.create_sale(user, "brand_1", 40)

    first = app.advance_service.run(user)
    second = app.advance_service.run(user)
    third = app.advance_service.run(user)

    assert first["sales_paid"] == 1
    assert second["sales_paid"] == 0     # already paid
    assert third["sales_paid"] == 0
    # Balance credited exactly once.
    assert app.get_wallet(user)["withdrawable_balance_paise"] == rupees_to_paise(4)


def test_new_pending_sale_gets_advance_on_next_run(app, user):
    app.create_sale(user, "brand_1", 40)
    app.advance_service.run(user)

    app.create_sale(user, "brand_2", 100)  # added later
    result = app.advance_service.run(user)

    assert result["sales_paid"] == 1
    assert result["total_advance_paise"] == rupees_to_paise(10)  # 10% of 100
    assert app.get_wallet(user)["withdrawable_balance_paise"] == rupees_to_paise(14)


def test_fractional_advance_uses_paise(app, user):
    app.create_sale(user, "brand_1", 45)  # 10% = ₹4.50 = 450 paise
    result = app.advance_service.run(user)
    assert result["total_advance_paise"] == 450
