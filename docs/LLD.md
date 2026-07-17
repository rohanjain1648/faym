# Low-Level Design — User Payout Management System

## 1. Problem summary

Affiliate sales enter as **pending**. The system pays each pending sale an
**advance** of 10% of its earnings. An admin later **reconciles** every sale to
**approved** or **rejected**, and the system computes the **final payout**
taking the already-paid advance into account. Users **withdraw** their
withdrawable balance (max one withdrawal per 24h), and any **failed / cancelled
/ rejected** payout is **refunded** so it can be withdrawn again.

## 2. The core financial model (the key idea)

Instead of computing a "final payout number" in isolation, the system maintains
a per-user **withdrawable balance** that is mutated by signed events. Two events
touch a sale's life:

| Event | Balance delta |
|-------|---------------|
| Advance paid on a pending sale | `+ 10% × earning` |
| Reconciled **approved** | `+ (earning − advance_paid)` |
| Reconciled **rejected** | `− advance_paid` |

Summing an approved sale: `+advance + (earning − advance) = +earning`.
Summing a rejected sale: `+advance − advance = 0`.

So after every sale is settled, the balance equals **the sum of approved
earnings** — exactly what the user is entitled to. The advance is just an early
partial release of that amount.

**Assignment worked example** (3 × ₹40, advance ₹4 each):

```
advance:   +4 +4 +4                = +12   (balance 12)
reconcile: reject -4, approve +36, approve +36 = +68  (balance 80)
```

- Balance = ₹80 = the two approved ₹40 sales. ✅
- "Final payout" beyond the advance = 80 − 12 = **₹68**. ✅

Withdrawals (`−amount`) and refunds (`+amount`) move the same balance.

## 3. Money representation

All amounts are stored and moved as **integer paise** (1 rupee = 100 paise).
Integer arithmetic in the smallest unit eliminates floating-point drift
(`0.1 + 0.2 ≠ 0.3`). The API accepts/returns rupees for readability and converts
at the boundary with `Decimal` + round-half-up (`app/money.py`). 10% of ₹45 is
correctly stored as 450 paise.

## 4. Data model

See [`schema.sql`](schema.sql) for the full DDL. Entities and relationships:

```
users 1─1 wallets
users 1─* sales *─1 brands
sales 1─0..1 advance_payouts     (UNIQUE sale_id  → idempotency)
sales 1─0..1 reconciliations     (UNIQUE sale_id  → settle once)
users 1─* withdrawals            (UNIQUE idempotency_key)
users 1─* ledger_entries         (append-only audit)
```

Key constraints:

- `advance_payouts.sale_id UNIQUE` — a sale can never be advanced twice.
- `reconciliations.sale_id UNIQUE` — a sale is settled exactly once.
- `sales.status`, `withdrawals.status` — `CHECK` constrained enums.
- `withdrawals.idempotency_key UNIQUE` — safe client retries.
- Indexes: `sales(user_id,status)`, `withdrawals(user_id,created_at)`,
  `ledger_entries(user_id,id)`.

**Ledger invariant:** `SUM(ledger_entries.amount_paise) == wallet balance` for
each user, verified in `tests/test_invariants.py`.

## 5. Class / module design

```
app/
  money.py        rupee⇄paise, advance = 10%
  constants.py    SaleStatus, WithdrawalStatus, LedgerType, rules
  exceptions.py   DomainError hierarchy (carry HTTP status codes)
  database.py     Database: shared SQLite conn + serialized transactions
  repositories.py stateless query helpers (one per table)
  services/
    wallet_service.py          single choke-point for balance + ledger
    advance_service.py         run() — idempotent advance job
    reconciliation_service.py  reconcile(), reconcile_batch()
    withdrawal_service.py      request(), update_status() (+ 24h clock)
  container.py    composition root + account/summary ops
  schemas.py      Pydantic request/response models
  main.py         FastAPI routes + domain-error handler
```

Design rationale:

- **Repository + Service + Composition-root** layering keeps business rules
  independent of both HTTP and storage. Swapping SQLite for Postgres touches
  only `database.py`/repositories; the services and tests are unchanged.
- **WalletService is the only writer of balances**, and it always writes a
  ledger row in the same transaction — the audit log cannot drift from reality.
- **Injectable clock** (`now_fn`) makes the 24h rule testable without waiting.

## 6. Concurrency, atomicity, idempotency

- Every money-moving operation runs inside `Database.transaction()`
  (`BEGIN IMMEDIATE` → commit/rollback). A whole operation (e.g. debit wallet +
  insert withdrawal + write ledger) is atomic; a failure rolls back all of it.
- A process-wide lock serializes access to the shared connection, preventing
  lost-update races on the balance. In production this maps to row locking /
  `SELECT … FOR UPDATE` on the wallet row in Postgres.
- **Idempotency** is enforced structurally, not just by application checks: the
  advance job relies on `UNIQUE(sale_id)` (catches `IntegrityError` and skips),
  and withdrawals use `UNIQUE(idempotency_key)`.

## 7. APIs

| Method & path | Purpose |
|---|---|
| `POST /users` | Create a user (+ wallet) |
| `POST /brands`, `GET /brands` | Register / list brands |
| `POST /sales` | Create a pending sale |
| `GET /sales/{id}`, `GET /sales?user_id=&status=` | Read sales |
| `POST /jobs/advance-payout` | Run the advance job (idempotent; per-user optional) |
| `POST /sales/{id}/reconcile` | Settle one sale (`approved`/`rejected`) |
| `POST /reconciliation/batch` | Settle many; per-item error isolation |
| `POST /users/{id}/withdrawals` | Request a withdrawal (24h + balance guards) |
| `GET  /users/{id}/withdrawals` | List withdrawals |
| `POST /withdrawals/{id}/status` | Provider callback; failure → auto-refund |
| `GET  /users/{id}/wallet` | Current balance |
| `GET  /users/{id}/ledger` | Full audit trail |
| `GET  /users/{id}/payout-summary` | Pending/approved/advance snapshot |

Errors return `{"error": {"code", "message"}}` with appropriate status codes
(404 not found, 409 conflict, 422 validation/insufficient-balance, 429 cooldown).

## 8. Edge cases & failure handling

| Case | Behaviour |
|---|---|
| Advance job run repeatedly | No duplicate advances (`UNIQUE(sale_id)`), balance credited once |
| Sale created after job ran | Picked up on the next run |
| Reconcile an already-settled sale | `409 Conflict` |
| Reconcile a sale with no advance | Works; `advance_paid = 0` |
| Invalid reconcile status | `422` |
| Withdraw > balance / ≤ 0 | `422` |
| Second withdrawal within 24h | `429` with `retry_after_seconds` |
| Withdrawal fails/cancelled/rejected | Refunded once; stops counting toward 24h; user can re-withdraw immediately |
| Double terminal transition | `409`; refund happens only once |
| Advance withdrawn, then sale rejected | Balance goes negative (claw-back debt), preserved in ledger |
| Client retries a withdrawal | `idempotency_key` returns the original, no double debit |

## 9. Trade-offs

- **SQLite + global lock** was chosen for a zero-setup, fully reproducible
  submission with correct transactions. It serializes writes (fine here); the
  same transaction boundaries and `SELECT … FOR UPDATE` semantics carry over to
  Postgres for real throughput.
- **Balance materialized on the wallet** (vs. summing the ledger every read)
  keeps reads O(1); the ledger provides auditability and a recomputation source
  of truth. Both are updated in one transaction so they can't diverge.
- **Negative balances allowed** to model claw-back debt honestly rather than
  hiding it; withdrawal guards still prevent spending money that isn't there.
- **Advance computed per-sale** (10% of each earning) rather than 10% of a
  bucket total — identical totals, but it gives every sale a concrete
  `advance_paid` to net against at reconciliation.
