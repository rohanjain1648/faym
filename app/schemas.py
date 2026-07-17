"""Pydantic request/response models for the API boundary.

Amounts are accepted/returned in **rupees** at the API for readability; the
core stores paise. Response models add a rupee mirror for money fields so
clients never have to divide by 100 themselves.
"""

from pydantic import BaseModel, Field

from .money import paise_to_rupees


# ---- requests ----------------------------------------------------------------

class CreateUserRequest(BaseModel):
    user_id: str = Field(..., examples=["john_doe"])


class CreateBrandRequest(BaseModel):
    brand_id: str = Field(..., examples=["brand_1"])
    name: str | None = None


class CreateSaleRequest(BaseModel):
    user_id: str = Field(..., examples=["john_doe"])
    brand: str = Field(..., examples=["brand_1"])
    earning: float = Field(..., ge=0, examples=[40])


class ReconcileRequest(BaseModel):
    status: str = Field(..., examples=["approved"], description="approved | rejected")


class BatchReconcileItem(BaseModel):
    sale_id: int
    status: str


class BatchReconcileRequest(BaseModel):
    items: list[BatchReconcileItem]


class WithdrawRequest(BaseModel):
    amount: float = Field(..., gt=0, examples=[80])
    idempotency_key: str | None = None


class WithdrawalStatusRequest(BaseModel):
    status: str = Field(..., examples=["failed"],
                        description="processing | completed | failed | cancelled | rejected")
    failure_reason: str | None = None


class RunAdvanceRequest(BaseModel):
    user_id: str | None = Field(
        None, description="Limit the job to one user; omit to run for everyone.")


# ---- response helper ---------------------------------------------------------

def with_rupees(data: dict) -> dict:
    """Attach a `*_rupees` mirror for every `*_paise` field in a dict."""
    out = dict(data)
    for key, value in list(data.items()):
        if key.endswith("_paise") and isinstance(value, int):
            out[key[:-6] + "_rupees"] = paise_to_rupees(value)
    return out
