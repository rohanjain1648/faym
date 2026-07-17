"""FastAPI application: HTTP surface over the payout domain.

Run:  uvicorn app.main:app --reload
Docs: http://127.0.0.1:8000/docs
"""

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .container import Container
from .exceptions import DomainError, WithdrawalCooldownError
from .schemas import (
    CreateUserRequest, CreateBrandRequest, CreateSaleRequest, ReconcileRequest,
    BatchReconcileRequest, WithdrawRequest, WithdrawalStatusRequest,
    RunAdvanceRequest, with_rupees,
)

app = FastAPI(
    title="User Payout Management System",
    version="1.0.0",
    description="Advance payouts, reconciliation, and failed-payout recovery "
                "for affiliate sales.",
)

# One shared container (single SQLite DB) for the process.
container = Container()


# ---- error handling ----------------------------------------------------------

@app.exception_handler(DomainError)
async def domain_error_handler(_request, exc: DomainError):
    body = {"error": {"code": exc.code, "message": exc.message}}
    if isinstance(exc, WithdrawalCooldownError):
        body["error"]["retry_after_seconds"] = exc.retry_after_seconds
    return JSONResponse(status_code=exc.status_code, content=body)


# ---- health ------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ---- users / brands / sales --------------------------------------------------

@app.post("/users", tags=["users"], status_code=201)
def create_user(req: CreateUserRequest):
    return with_rupees(container.create_user(req.user_id))


@app.get("/users/{user_id}/wallet", tags=["users"])
def get_wallet(user_id: str):
    return with_rupees(container.get_wallet(user_id))


@app.get("/users/{user_id}/ledger", tags=["users"])
def get_ledger(user_id: str):
    return [with_rupees(e) for e in container.get_ledger(user_id)]


@app.get("/users/{user_id}/payout-summary", tags=["users"])
def payout_summary(user_id: str):
    return with_rupees(container.payout_summary(user_id))


@app.post("/brands", tags=["brands"], status_code=201)
def create_brand(req: CreateBrandRequest):
    return container.create_brand(req.brand_id, req.name)


@app.get("/brands", tags=["brands"])
def list_brands():
    return container.list_brands()


@app.post("/sales", tags=["sales"], status_code=201)
def create_sale(req: CreateSaleRequest):
    return with_rupees(container.create_sale(req.user_id, req.brand, req.earning))


@app.get("/sales/{sale_id}", tags=["sales"])
def get_sale(sale_id: int):
    return with_rupees(container.get_sale(sale_id))


@app.get("/sales", tags=["sales"])
def list_sales(user_id: str = Query(...), status: str | None = Query(None)):
    return [with_rupees(s) for s in container.list_sales(user_id, status)]


# ---- advance payout job ------------------------------------------------------

@app.post("/jobs/advance-payout", tags=["jobs"])
def run_advance_payout(req: RunAdvanceRequest | None = None):
    user_id = req.user_id if req else None
    return with_rupees(container.advance_service.run(user_id))


# ---- reconciliation ----------------------------------------------------------

@app.post("/sales/{sale_id}/reconcile", tags=["reconciliation"])
def reconcile(sale_id: int, req: ReconcileRequest):
    return with_rupees(container.reconciliation_service.reconcile(sale_id, req.status))


@app.post("/reconciliation/batch", tags=["reconciliation"])
def reconcile_batch(req: BatchReconcileRequest):
    items = [i.model_dump() for i in req.items]
    result = container.reconciliation_service.reconcile_batch(items)
    result["reconciled"] = [with_rupees(r) for r in result["reconciled"]]
    return result


# ---- withdrawals -------------------------------------------------------------

@app.post("/users/{user_id}/withdrawals", tags=["withdrawals"], status_code=201)
def request_withdrawal(user_id: str, req: WithdrawRequest):
    from .money import rupees_to_paise
    return with_rupees(container.withdrawal_service.request(
        user_id, rupees_to_paise(req.amount), req.idempotency_key))


@app.get("/users/{user_id}/withdrawals", tags=["withdrawals"])
def list_withdrawals(user_id: str):
    return [with_rupees(w) for w in container.withdrawal_service.list_for_user(user_id)]


@app.post("/withdrawals/{withdrawal_id}/status", tags=["withdrawals"])
def update_withdrawal_status(withdrawal_id: int, req: WithdrawalStatusRequest):
    return with_rupees(container.withdrawal_service.update_status(
        withdrawal_id, req.status, req.failure_reason))
