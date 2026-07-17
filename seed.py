"""Seed a fresh database with the assignment's reference data and walk through
the full lifecycle so you can eyeball the numbers.

Usage:  python seed.py
It uses a separate 'seed_demo.db' so it never clobbers the API's payouts.db.
"""

import sys

from app.database import Database
from app.container import Container
from app.money import paise_to_rupees

# The ₹ symbol needs UTF-8; Windows consoles default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def rupees(paise):
    return f"₹{paise_to_rupees(paise):g}"


def main():
    db = Database("seed_demo.db")
    # Start clean each run.
    with db.transaction() as conn:
        for table in ("ledger_entries", "reconciliations", "advance_payouts",
                      "withdrawals", "sales", "wallets", "users", "brands"):
            conn.execute(f"DELETE FROM {table}")

    app = Container(db=db)

    for b in ("brand_1", "brand_2", "brand_3"):
        app.create_brand(b)
    app.create_user("john_doe")

    print("== Create 3 pending sales @ ₹40 (brand_1) ==")
    sale_ids = [app.create_sale("john_doe", "brand_1", 40)["id"] for _ in range(3)]
    print("  sale ids:", sale_ids)

    summary = app.payout_summary("john_doe")
    print(f"  pending earnings: {rupees(summary['pending_earnings_paise'])}, "
          f"eligible advance: {rupees(summary['eligible_advance_now_paise'])}")

    print("\n== Run advance payout job (twice, to prove idempotency) ==")
    r1 = app.advance_service.run("john_doe")
    r2 = app.advance_service.run("john_doe")
    print(f"  run 1 paid {r1['sales_paid']} sales, total {rupees(r1['total_advance_paise'])}")
    print(f"  run 2 paid {r2['sales_paid']} sales (should be 0)")
    print(f"  wallet: {rupees(app.get_wallet('john_doe')['withdrawable_balance_paise'])}")

    print("\n== Reconcile: reject, approve, approve ==")
    app.reconciliation_service.reconcile(sale_ids[0], "rejected")
    app.reconciliation_service.reconcile(sale_ids[1], "approved")
    app.reconciliation_service.reconcile(sale_ids[2], "approved")
    bal = app.get_wallet("john_doe")["withdrawable_balance_paise"]
    print(f"  wallet after reconciliation: {rupees(bal)}  (expected ₹80)")
    print(f"  final payout beyond advance = {rupees(bal - r1['total_advance_paise'])}"
          f"  (expected ₹68)")

    print("\n== Withdraw ₹80 ==")
    wd = app.withdrawal_service.request("john_doe", bal)
    print(f"  withdrawal #{wd['id']} status={wd['status']} "
          f"wallet={rupees(wd['wallet_balance_paise'])}")

    print("\n== Provider FAILS the payout -> auto refund ==")
    res = app.withdrawal_service.update_status(wd["id"], "failed", "bank timeout")
    print(f"  refunded={res['refunded']} wallet={rupees(res['wallet_balance_paise'])}")

    print("\n== Ledger ==")
    for e in app.get_ledger("john_doe"):
        print(f"  {e['entry_type']:<24} {rupees(e['amount_paise']):>7}  "
              f"-> balance {rupees(e['balance_after_paise'])}")


if __name__ == "__main__":
    main()
