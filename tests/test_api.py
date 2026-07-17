"""End-to-end tests over the HTTP API using FastAPI's TestClient."""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.database import Database
from app.container import Container


@pytest.fixture
def client(tmp_path):
    # Point the app at an isolated DB for the test.
    main.container = Container(db=Database(tmp_path / "api.db"))
    return TestClient(main.app)


def test_full_lifecycle_over_http(client):
    assert client.get("/health").json() == {"status": "ok"}

    client.post("/users", json={"user_id": "john_doe"})
    for _ in range(3):
        r = client.post("/sales",
                        json={"user_id": "john_doe", "brand": "brand_1", "earning": 40})
        assert r.status_code == 201

    # Advance job.
    r = client.post("/jobs/advance-payout", json={"user_id": "john_doe"})
    assert r.json()["total_advance_rupees"] == 12.0

    # Reconcile reject / approve / approve.
    client.post("/sales/1/reconcile", json={"status": "rejected"})
    client.post("/sales/2/reconcile", json={"status": "approved"})
    client.post("/sales/3/reconcile", json={"status": "approved"})

    wallet = client.get("/users/john_doe/wallet").json()
    assert wallet["withdrawable_balance_rupees"] == 80.0

    # Withdraw ₹80, then fail it -> refunded.
    wd = client.post("/users/john_doe/withdrawals", json={"amount": 80}).json()
    assert wd["wallet_balance_rupees"] == 0.0
    res = client.post(f"/withdrawals/{wd['id']}/status",
                      json={"status": "failed", "failure_reason": "bank"}).json()
    assert res["refunded"] is True
    assert client.get("/users/john_doe/wallet").json()["withdrawable_balance_rupees"] == 80.0


def test_cooldown_returns_429(client):
    client.post("/users", json={"user_id": "jane"})
    # Fund ₹100 via an approved sale.
    client.post("/sales", json={"user_id": "jane", "brand": "brand_1", "earning": 100})
    client.post("/sales/1/reconcile", json={"status": "approved"})

    assert client.post("/users/jane/withdrawals", json={"amount": 30}).status_code == 201
    second = client.post("/users/jane/withdrawals", json={"amount": 30})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "withdrawal_cooldown"


def test_insufficient_balance_returns_422(client):
    client.post("/users", json={"user_id": "poor"})
    r = client.post("/users/poor/withdrawals", json={"amount": 10})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "insufficient_balance"
