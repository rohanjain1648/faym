# User Payout Management System

A Low-Level Design **and** working implementation of a payout system for
affiliate sales: advance payouts, admin reconciliation, final-payout
calculation, 24-hour withdrawal limits, and failed-payout recovery.

Built with **Python 3.12 + FastAPI + SQLite**. All money is handled as
integer paise (never floats) to make the arithmetic exact and auditable.

> This README explains the *what* and *why*. The line-by-line schema lives in
> [`docs/schema.sql`](docs/schema.sql); a shorter design summary lives in
> [`docs/LLD.md`](docs/LLD.md). Nothing in those files contradicts this one —
> this is the deep-dive version.

---

## Table of contents

1. [Problem, restated](#1-problem-restated)
2. [The core financial model](#2-the-core-financial-model)
3. [Why integer paise](#3-why-integer-paise)
4. [Architecture: the four layers](#4-architecture-the-four-layers)
5. [Data model](#5-data-model)
6. [Request lifecycle — three worked traces](#6-request-lifecycle--three-worked-traces)
7. [Concurrency, atomicity, idempotency](#7-concurrency-atomicity-idempotency)
8. [Business rules, rule by rule](#8-business-rules-rule-by-rule)
9. [API reference](#9-api-reference)
10. [Edge cases & failure handling](#10-edge-cases--failure-handling)
11. [Testing strategy](#11-testing-strategy)
12. [Design decisions & trade-offs](#12-design-decisions--trade-offs)
13. [What I'd change for production scale](#13-what-id-change-for-production-scale)
14. [Project layout](#14-project-layout)
15. [Quick start](#15-quick-start)

---

## 1. Problem, restated

Every affiliate sale starts as **pending**. Two independent processes act on it
over time:

- **Advance payout job** (runs on a schedule, or on demand): pays the user 10%
  of the earnings on every pending sale that hasn't been advanced yet.
- **Admin reconciliation** (happens later, manually, per sale): moves a
  pending sale to **approved** (delivered, return window closed — the user
  earned it) or **rejected** (returned/cancelled — the user earned nothing).

The system has to reconcile these two independent timelines into one number:
what the user can actually withdraw. On top of that:

- Withdrawals are capped at **one per 24 hours** per user.
- If a withdrawal is later **cancelled / rejected / failed** by the payment
  provider, the money must come back to the user's withdrawable balance so
  they can try again.

The hard part isn't the arithmetic — it's making the arithmetic **correct no
matter what order things happen in, and safe to repeat**. The advance job might
run five times before reconciliation happens. Reconciliation might happen
before the advance job ever ran. A withdrawal might be retried by a flaky
client. All of these have to converge on the same, correct balance.

## 2. The core financial model

Rather than compute "final payout" as a one-off number at reconciliation time,
the system maintains a single **withdrawable balance per user**, mutated by a
stream of signed events. Every event is also written to an **append-only
ledger**, so the balance is always both a live number and a fully
reconstructable audit trail.

Four event types touch the balance:

| Event | Balance delta | When it happens |
|---|---|---|
| Advance paid | `+10% × earning` | Advance job runs over a pending sale |
| Reconciled **approved** | `+(earning − advance_paid)` | Admin approves the sale |
| Reconciled **rejected** | `−advance_paid` | Admin rejects the sale |
| Withdrawal requested | `−amount` | User withdraws |
| Withdrawal fails/cancels/rejects | `+amount` | Provider reports failure |

**Why this converges correctly.** Trace one sale through its whole life,
summing the deltas it contributes:

- *Approved sale*: `+advance + (earning − advance) = +earning`. The user ends
  up with exactly the earning, split into two payments (advance now, remainder
  at reconciliation) instead of one.
- *Rejected sale*: `+advance − advance = 0`. The advance was a temporary loan;
  the claw-back at reconciliation cancels it out exactly.

So **after every sale is settled, the wallet balance equals the sum of
approved earnings** — precisely what the business intends the user to have.
Nothing about this depends on the order of advance-vs-reconciliation, because
each sale's own `advance_paid` (looked up from the `advance_payouts` table, not
assumed) is what gets netted against it. If reconciliation happens before any
advance was ever paid, `advance_paid = 0` and the approved sale simply credits
the full earning — same formula, no special case needed.

**Worked example** (the one from the assignment): three sales at ₹40 each,
10% advance on each, then reject/approve/approve:

```
create 3 pending sales @ ₹40         →  pending earnings ₹120
advance job (10% each)               →  +4 +4 +4         = +12   → balance ₹12
reconcile: reject, approve, approve  →  -4 +36 +36        = +68   → balance ₹80
```

`₹80` is exactly the sum of the two approved ₹40 sales. The "final payout"
number the assignment asks for — the amount beyond what was already
advanced — is `80 − 12 = ₹68`, matching the spec exactly. This is verified
byte-for-byte in [`tests/test_reconciliation.py::test_assignment_worked_example_totals_68`](tests/test_reconciliation.py)
and printed live by `python seed.py`.

## 3. Why integer paise

Every amount in the database and in service-layer arithmetic is an `INTEGER`
count of paise (1 rupee = 100 paise), not a float and not even a `Decimal` at
rest. Two reasons:

1. **Binary floats can't represent most decimal fractions exactly**
   (`0.1 + 0.2 == 0.30000000000000004` in IEEE-754). Summing thousands of
   payout fractions in float would eventually produce a balance that's off by
   fractions of a paisa — invisible in testing, real in production.
2. **10% of an odd rupee amount is fractional** (10% of ₹45 = ₹4.50 = 450
   paise). Doing this in integer paise with explicit rounding
   (`ROUND_HALF_UP` via `Decimal`, see [`app/money.py`](app/money.py)) gives a
   single, deterministic, auditable rounding rule instead of whatever the
   platform's float rounding happens to do.

The API boundary is the only place rupees appear: requests accept a rupee
`float` for human readability, `app/money.py::rupees_to_paise()` converts it to
paise using `Decimal` before it touches the database, and every response
mirrors each `*_paise` field with a computed `*_rupees` field
(`app/schemas.py::with_rupees()`) so API consumers never have to divide by 100
themselves.

## 4. Architecture: the four layers

```
┌─────────────────────────────────────────────────────────────────┐
│  HTTP layer            app/main.py, app/schemas.py               │
│  FastAPI routes ↔ Pydantic models ↔ domain-error → HTTP status   │
├─────────────────────────────────────────────────────────────────┤
│  Composition root       app/container.py                        │
│  Wires repositories + services over one Database; exposes        │
│  account/catalog ops (create user, list sales, payout summary)   │
├─────────────────────────────────────────────────────────────────┤
│  Service layer          app/services/*.py                       │
│  Business rules. Each service takes repositories as              │
│  constructor args and orchestrates them inside one transaction.  │
│    WalletService          – the only writer of balance + ledger  │
│    AdvancePayoutService   – the idempotent advance job           │
│    ReconciliationService  – settles a sale, computes the delta   │
│    WithdrawalService      – 24h cooldown, balance guard, refunds │
├─────────────────────────────────────────────────────────────────┤
│  Data access layer      app/repositories.py, app/database.py    │
│  Stateless SQL per table. Database owns the shared SQLite        │
│  connection, the schema, and `transaction()` / `read()` scopes.  │
└─────────────────────────────────────────────────────────────────┘
```

**Why this shape.** Each layer only knows about the layer directly below it:

- Routes never touch SQL — they call container/service methods and translate
  `DomainError` subclasses into HTTP responses via one exception handler
  (`app/main.py::domain_error_handler`). Adding a new endpoint never means
  duplicating a business rule.
- Services never touch FastAPI or Pydantic — they raise plain Python
  exceptions (`NotFoundError`, `ConflictError`, `ValidationError`,
  `InsufficientBalanceError`, `WithdrawalCooldownError`, all in
  [`app/exceptions.py`](app/exceptions.py)) and work entirely in paise. This is
  what lets `tests/test_advance_payout.py`, `test_reconciliation.py`,
  `test_withdrawal.py`, and `test_recovery.py` exercise every business rule
  directly against `Container`, with **no HTTP server involved** — faster
  tests, and failures point straight at the business logic instead of routing
  glue.
- Repositories never contain business logic — each method is close to a raw
  SQL statement (`SaleRepository.pending_without_advance`,
  `WithdrawalRepository.latest_active`, etc.) parameterized by the caller.
  Swapping SQLite for Postgres means rewriting `app/database.py`'s connection
  handling and, at most, tweaking a handful of repository queries — the
  services and every test above them are untouched.
- `Container` (the composition root) is the one place that knows how to wire
  a `Database` into eight repositories and four services. Production code
  builds one `Container()` at process start (`app/main.py`); tests build a
  fresh one per test pointed at a temp-file database
  (`tests/conftest.py::app` fixture) — full isolation with one line.

**Single choke-point for money.** Every balance change — advance credit,
reconciliation adjustment, withdrawal debit, refund credit — flows through
`WalletService.apply()` ([`app/services/wallet_service.py`](app/services/wallet_service.py)).
It does two things atomically: update `wallets.withdrawable_balance_paise`,
and insert a row into `ledger_entries`. No other code path is allowed to touch
`wallets` directly. This is what makes the invariant
`SUM(ledger_entries.amount_paise) == wallets.withdrawable_balance_paise`
unconditionally true — it isn't a property the code happens to have, it's
structurally impossible to violate without going around the one function that
writes balances. [`tests/test_invariants.py`](tests/test_invariants.py) asserts
this after a chain of advance → reconcile → withdraw → refund operations.

## 5. Data model

Full DDL: [`docs/schema.sql`](docs/schema.sql). Entity relationships:

```
users ──1:1── wallets
  │
  ├──1:N── sales ──N:1── brands
  │          │
  │          ├──1:0..1── advance_payouts     (UNIQUE sale_id)
  │          └──1:0..1── reconciliations     (UNIQUE sale_id)
  │
  ├──1:N── withdrawals                       (UNIQUE idempotency_key)
  │
  └──1:N── ledger_entries                    (append-only)
```

| Table | Purpose | Key constraints |
|---|---|---|
| `users` | Affiliate accounts | PK `id` (natural key, e.g. `john_doe`) |
| `brands` | Sale categories | PK `id` |
| `wallets` | One withdrawable balance per user | PK `user_id`, may be **negative** (see §10) |
| `sales` | One row per sale, lifecycle `pending → approved/rejected` | `status CHECK IN (...)`, `earning_paise >= 0`, index on `(user_id, status)` |
| `advance_payouts` | One row per advance actually paid | **`sale_id UNIQUE`** — the idempotency guard for rule #1 |
| `reconciliations` | One row per settlement, records the signed delta | **`sale_id UNIQUE`** — a sale settles exactly once |
| `withdrawals` | Payout requests and their provider status | `idempotency_key UNIQUE`, `status CHECK IN (...)`, index on `(user_id, created_at)` for the cooldown lookup |
| `ledger_entries` | Every wallet movement, ever | Append-only, index on `(user_id, id)` |

Two `UNIQUE` constraints are load-bearing, not incidental:

- `advance_payouts.sale_id UNIQUE` is what makes the advance job safe to run
  any number of times. The job's own query
  (`SaleRepository.pending_without_advance`, a `LEFT JOIN ... WHERE a.id IS
  NULL`) already filters out sales with an existing advance, but the unique
  constraint is the **hard backstop**: even under a race (two job runs
  overlapping), the second `INSERT` throws `sqlite3.IntegrityError`, which
  `AdvancePayoutService.run()` catches and treats as "already paid, skip."
  Correctness doesn't depend on the query being race-free — it depends on the
  constraint.
- `withdrawals.idempotency_key UNIQUE` gives withdrawal requests the same
  guarantee for network retries: if a client's request times out and it
  retries with the same key, `WithdrawalService.request()` finds the existing
  row and returns it unchanged instead of debiting twice.

## 6. Request lifecycle — three worked traces

### Trace A: the advance payout job

`POST /jobs/advance-payout {"user_id": "john_doe"}`

1. `app/main.py::run_advance_payout` parses the body, calls
   `container.advance_service.run(user_id)`.
2. `AdvancePayoutService.run()` opens `Database.transaction()` — everything
   below happens inside one `BEGIN IMMEDIATE` ... `COMMIT`.
3. `SaleRepository.pending_without_advance(conn, user_id)` — a `LEFT JOIN`
   against `advance_payouts` — returns every pending sale with no advance row.
4. For each sale: `advance_amount_paise(earning_paise)` computes 10%
   (`Decimal`, round-half-up). `AdvancePayoutRepository.create()` attempts the
   insert; if it raises `IntegrityError` (a concurrent run beat us to it), the
   sale is skipped with no side effect. Otherwise `WalletService.apply()`
   credits the wallet and writes a `ledger_entries` row with
   `entry_type='advance_credit'`, `ref_type='sale'`, `ref_id=<sale id>`.
5. Transaction commits. Response: `{"sales_paid": N, "total_advance_paise":
   ..., "details": [...]}`, mirrored to rupees by `with_rupees()`.

Run the same request again with the same sales already advanced: step 3
returns an empty list (the `LEFT JOIN` now excludes them), `sales_paid: 0`,
balance unchanged. This is exercised in
[`tests/test_advance_payout.py::test_advance_job_is_idempotent`](tests/test_advance_payout.py).

### Trace B: reconciling a sale

`POST /sales/2/reconcile {"status": "approved"}`

1. `app/main.py::reconcile` calls
   `container.reconciliation_service.reconcile(2, "approved")`.
2. `ReconciliationService.reconcile()` validates `new_status ∈
   {approved, rejected}` (else `ValidationError → 422`), opens a transaction.
3. Loads the sale. Not found → `NotFoundError → 404`. Already settled
   (`status != 'pending'`) → `ConflictError → 409` — a sale can only be
   reconciled once, enforced both here and by `reconciliations.sale_id
   UNIQUE`.
4. Looks up any existing advance for this sale
   (`AdvancePayoutRepository.get_for_sale`); `advance_paid = 0` if none exists.
5. Computes the signed adjustment: `earning − advance_paid` if approved,
   `−advance_paid` if rejected (§2).
6. `SaleRepository.mark_reconciled()` flips the sale's status.
   `ReconciliationRepository.create()` records the settlement (for audit / to
   enforce "settle once" via its own unique constraint).
   `WalletService.apply()` applies the delta and writes the ledger row
   (`entry_type='reconciliation_adjustment'`).
7. Commit. Response includes `earning_paise`, `advance_paid_paise`,
   `adjustment_paise`, and the resulting `wallet_balance_paise` — everything
   needed to verify the math without a second query.

### Trace C: withdrawal → provider failure → refund

`POST /users/john_doe/withdrawals {"amount": 80}` then
`POST /withdrawals/1/status {"status": "failed", "failure_reason": "bank timeout"}`

1. `WithdrawalService.request()`: if an `idempotency_key` was supplied and
   already exists, return that row untouched (no double debit) — see §7.
   Otherwise `_enforce_cooldown()` looks up the user's most recent *active*
   withdrawal (`status IN (initiated, processing, completed)`) via
   `WithdrawalRepository.latest_active`, using a **service-owned clock**
   (`self._now()`, injectable — see §7) rather than SQLite's own
   `datetime('now')`, and raises `WithdrawalCooldownError → 429` if under 24h.
2. Balance check: `amount_paise > current balance` → `InsufficientBalanceError
   → 422`.
3. Insert the withdrawal (`status='initiated'`), debit the wallet via
   `WalletService.apply()` (`entry_type='withdrawal_debit'`, negative amount).
   The money leaves the withdrawable balance the instant the withdrawal is
   *requested*, not when the provider confirms — otherwise a user could
   request the same balance twice before the provider responds.
4. Later, the provider calls back:
   `WithdrawalService.update_status(1, "failed", "bank timeout")`. If the
   withdrawal is already `TERMINAL` (completed/failed/cancelled/rejected),
   reject with `ConflictError → 409` — a terminal state can't be revisited, so
   a refund can never double-fire.
5. Status flips to `failed`. Because `failed ∈ WithdrawalStatus.RECOVERABLE`,
   `WalletService.apply()` credits the amount straight back
   (`entry_type='withdrawal_refund'`). The response includes `refunded: true`
   and the new balance.
6. Because the withdrawal is now terminal, it's excluded from
   `latest_active()` — the user can request a new withdrawal for the same
   amount **immediately**, with no 24h wait, since the failed attempt never
   actually delivered money.

## 7. Concurrency, atomicity, idempotency

- **Atomicity.** `Database.transaction()` wraps `BEGIN IMMEDIATE` /
  `COMMIT` / `ROLLBACK` around every write operation
  ([`app/database.py`](app/database.py)). A multi-step operation — e.g.
  "insert withdrawal row + debit wallet + write ledger entry" — either
  completes entirely or leaves no trace. If any step raises, the whole
  transaction rolls back.
- **Serialization.** A process-wide `threading.RLock` guards the single
  shared SQLite connection, so concurrent requests can't interleave writes and
  produce a lost update on the balance. This is the SQLite-appropriate
  analogue of `SELECT ... FOR UPDATE` on the wallet row in Postgres — same
  correctness property, cheaper to set up for a single-process assignment
  submission.
- **Idempotency is structural, not just logical.** Two independent unique
  constraints back two independent business requirements: `UNIQUE(sale_id)`
  on `advance_payouts` (rule #1: never double-advance) and
  `UNIQUE(idempotency_key)` on `withdrawals` (safe client retries). Both are
  enforced by SQLite itself, so even a bug in the application-level "already
  paid?" check can't corrupt the balance — the database refuses the duplicate
  row outright.
- **Testable time.** The 24h cooldown can't be tested by actually waiting 24
  hours, so `WithdrawalService` takes an injectable `now_fn` (defaults to
  `datetime.now(UTC)`). Tests pass a `FakeClock`
  ([`tests/conftest.py`](tests/conftest.py)) that can be advanced
  deterministically (`clock.advance(hours=24, minutes=1)`), and the service
  writes its *own* clock's timestamp into `withdrawals.created_at` (rather
  than trusting SQLite's `datetime('now')`) so the cooldown comparison and the
  stored timestamp always agree.

## 8. Business rules, rule by rule

### Rule 1 — Advance payout (10%, exactly once)

> "Once an advance payout has been successfully transferred, the same sale
> must never receive another advance payout, even if the advance payout job
> runs multiple times."

Implemented by `AdvancePayoutService.run()` (§6, Trace A). Verified for: a
single sale, three sales summed, running the job 2–3 times back to back, and a
new pending sale created *after* an earlier job run (it gets picked up next
run, but only once) — see
[`tests/test_advance_payout.py`](tests/test_advance_payout.py).

### Rule 2 — Final payout on reconciliation

> Approved: `earning − advance_paid`. Rejected: `−advance_paid` (claw-back).

Implemented by `ReconciliationService.reconcile()` (§6, Trace B). The two
cases from the spec (`₹30 earning / ₹3 advance → +₹27` on approval; `₹50
earning / ₹5 advance → −₹5` on rejection) and the full three-sale worked
example (`→ ₹68`) are each individual test functions in
[`tests/test_reconciliation.py`](tests/test_reconciliation.py), plus a test for
reconciling a sale that never got an advance (`advance_paid = 0`, full earning
credited — no special-casing needed, see §2) and a test that a sale cannot be
reconciled twice.

### Rule 3 — One withdrawal per 24 hours

Implemented by `WithdrawalService._enforce_cooldown()` (§6, Trace C), looking
at the most recent *active* withdrawal only — so a withdrawal that later fails
doesn't keep blocking the window (that's Question 2, next). Tested for:
blocked one hour later, allowed after 24h+1m, and (in the recovery tests)
allowed immediately after a failure. See
[`tests/test_withdrawal.py`](tests/test_withdrawal.py).

### Question 2 — Failed payout recovery

> Cancelled / rejected / failed payouts credit the amount back to the
> withdrawable balance and allow another withdrawal.

Implemented by `WithdrawalService.update_status()` (§6, Trace C, steps 4–6).
`WithdrawalStatus.RECOVERABLE = {failed, cancelled, rejected}` triggers exactly
one refund; `WithdrawalStatus.TERMINAL` (which is a superset, adding
`completed`) blocks any further status transition, which is what guarantees
the refund fires **at most once** per withdrawal. Tested for all three
recoverable terminal states (parametrized test), for the "completed, no
refund" case, for "double failure only refunds once," and for "no cooldown
after a refunded failure." See
[`tests/test_recovery.py`](tests/test_recovery.py).

## 9. API reference

All endpoints return JSON. Money fields are doubled: `*_paise` (source of
truth, integer) and `*_rupees` (float, for display) — both are present on every
money-bearing response field.

| Method & path | Body | Purpose |
|---|---|---|
| `GET /health` | — | Liveness check |
| `POST /users` | `{user_id}` | Create a user (+ empty wallet) |
| `GET /users/{id}/wallet` | — | Current withdrawable balance |
| `GET /users/{id}/ledger` | — | Full, ordered audit trail |
| `GET /users/{id}/payout-summary` | — | Pending/approved/rejected counts, pending earnings, advance already paid, advance still due, current balance — one snapshot |
| `POST /brands` | `{brand_id, name?}` | Register/update a brand |
| `GET /brands` | — | List brands |
| `POST /sales` | `{user_id, brand, earning}` | Create a pending sale (auto-creates user/brand if new — convenience for demos) |
| `GET /sales/{id}` | — | Fetch one sale |
| `GET /sales?user_id=&status=` | — | List a user's sales, optionally filtered by status |
| `POST /jobs/advance-payout` | `{user_id?}` | Run the advance job — for one user, or (omit `user_id`) for everyone |
| `POST /sales/{id}/reconcile` | `{status}` | Settle one sale to `approved`/`rejected` |
| `POST /reconciliation/batch` | `{items: [{sale_id, status}]}` | Settle many sales; each item succeeds/fails independently (one bad `sale_id` doesn't roll back the rest) |
| `POST /users/{id}/withdrawals` | `{amount, idempotency_key?}` | Request a withdrawal (24h + balance guards) |
| `GET /users/{id}/withdrawals` | — | List a user's withdrawals, newest first |
| `POST /withdrawals/{id}/status` | `{status, failure_reason?}` | Provider callback — moves the withdrawal to `processing`/`completed`/`failed`/`cancelled`/`rejected`; the last three auto-refund |

**Error shape**, uniform across every endpoint:

```json
{"error": {"code": "insufficient_balance", "message": "Requested ... but withdrawable balance is ..."}}
```

`WithdrawalCooldownError` additionally includes `"retry_after_seconds"`.

| HTTP status | `error.code` | Raised when |
|---|---|---|
| 404 | `not_found` | Unknown user / sale / withdrawal id |
| 409 | `conflict` | Re-reconciling a settled sale; changing a terminal withdrawal |
| 422 | `validation_error` | Bad `status` value, non-positive amount |
| 422 | `insufficient_balance` | Withdrawal amount exceeds current balance |
| 429 | `withdrawal_cooldown` | Second withdrawal inside 24h |

### End-to-end curl walkthrough

```bash
BASE=http://127.0.0.1:8000

# user + three ₹40 pending sales
curl -s -X POST $BASE/users -H 'content-type: application/json' -d '{"user_id":"john_doe"}'
for i in 1 2 3; do
  curl -s -X POST $BASE/sales -H 'content-type: application/json' \
       -d '{"user_id":"john_doe","brand":"brand_1","earning":40}'
done

# advance job → total_advance_rupees = 12
curl -s -X POST $BASE/jobs/advance-payout -H 'content-type: application/json' -d '{"user_id":"john_doe"}'

# reconcile: reject #1, approve #2 and #3
curl -s -X POST $BASE/sales/1/reconcile -H 'content-type: application/json' -d '{"status":"rejected"}'
curl -s -X POST $BASE/sales/2/reconcile -H 'content-type: application/json' -d '{"status":"approved"}'
curl -s -X POST $BASE/sales/3/reconcile -H 'content-type: application/json' -d '{"status":"approved"}'

# wallet → withdrawable_balance_rupees = 80
curl -s $BASE/users/john_doe/wallet

# withdraw ₹80, then simulate a provider failure → refunded
WID=$(curl -s -X POST $BASE/users/john_doe/withdrawals \
     -H 'content-type: application/json' -d '{"amount":80}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST $BASE/withdrawals/$WID/status \
     -H 'content-type: application/json' -d '{"status":"failed","failure_reason":"bank timeout"}'

# full audit trail
curl -s $BASE/users/john_doe/ledger
```

## 10. Edge cases & failure handling

| Case | Behaviour | Why |
|---|---|---|
| Advance job run 2+ times | No duplicate advance; `sales_paid: 0` on repeats | `UNIQUE(sale_id)` + work-list filter (§5, §6A) |
| New pending sale added after a job run | Picked up on the *next* run only | Work-list query is re-evaluated fresh each call |
| Reconcile a sale twice | `409 Conflict`, no second delta applied | `status != 'pending'` guard + `reconciliations.sale_id UNIQUE` |
| Reconcile a sale that never got an advance | Works; `advance_paid = 0`, full earning credited | Delta formula needs no special case (§2) |
| Invalid `status` on reconcile | `422 Validation` | Explicit `∈ {approved, rejected}` check |
| Reconcile unknown `sale_id` | `404 Not Found` | Explicit lookup before mutation |
| Batch reconcile with one bad item | Good items settle; bad item reported in `errors[]` | Each item wrapped in its own try/except, no shared transaction across items |
| Withdraw more than balance | `422 insufficient_balance` | Balance checked inside the same transaction as the debit |
| Withdraw ≤ ₹0 | `422 validation_error` | Explicit `amount_paise <= 0` check |
| Second withdrawal inside 24h | `429`, with `retry_after_seconds` | `_enforce_cooldown()` against the latest *active* withdrawal |
| Withdrawal fails/cancels/rejects | Refunded exactly once; no longer blocks the 24h window | `RECOVERABLE` triggers one refund; refunded withdrawal drops out of `latest_active()` |
| Same withdrawal reported terminal twice | Second call `409 Conflict`; refund not repeated | `TERMINAL` guard on `update_status()` |
| Advance withdrawn, *then* the sale is rejected | Balance goes **negative** (a claw-back debt), fully visible in the ledger | See below — modeled explicitly, not hidden |
| Client retries a withdrawal request | Same withdrawal returned, no second debit | `idempotency_key UNIQUE` short-circuits `request()` |
| Concurrent advance-job runs (race) | Second `INSERT` hits `IntegrityError`, is caught and skipped | `UNIQUE(sale_id)` is the actual guarantee, not just the pre-check query |

**On negative balances.** If a user is advanced ₹5 on a pending sale,
withdraws that ₹5 immediately, and the sale is *then* rejected, the system owes
a ₹5 claw-back it can no longer take from an already-zero balance. Rather than
silently clamping the balance at zero (which would make the ledger stop
summing to the balance — breaking the core invariant) or throwing an error
(which would make legitimate reconciliation fail), the wallet is allowed to go
negative, representing a real debt the user owes back. This is a deliberate,
tested choice
([`tests/test_invariants.py::test_balance_can_go_negative_on_clawback_after_withdrawal`](tests/test_invariants.py))
— the withdrawal *guard* still prevents new withdrawals from a negative or
insufficient balance; only the claw-back itself is allowed to cross zero,
because it isn't optional (the money was never rightfully the user's).

## 11. Testing strategy

**31 tests, `pytest -q`.** Split by what they exercise:

- `test_advance_payout.py` — Rule 1 in isolation, against `Container`
  directly (no HTTP).
- `test_reconciliation.py` — Rule 2, including both spec examples verbatim and
  the full three-sale worked example.
- `test_withdrawal.py` — Rule 3, using the injectable `FakeClock` to test the
  24h boundary without sleeping.
- `test_recovery.py` — Question 2, parametrized across `failed` / `cancelled`
  / `rejected`, plus the "refund exactly once" and "no cooldown after refund"
  guarantees.
- `test_invariants.py` — cross-cutting properties that must hold after *any*
  sequence of operations: `Σ ledger == wallet balance`, and the negative-balance
  claw-back case.
- `test_api.py` — the same lifecycle driven through FastAPI's `TestClient`
  instead of `Container` directly, confirming the HTTP layer (status codes,
  `error.code` values, request/response shapes) matches the domain layer
  correctly, using an isolated temp-file database per test.

Each test in `test_advance_payout.py` through `test_recovery.py` runs against
a **fresh temp-file SQLite database** (`tests/conftest.py::app` fixture, backed
by pytest's `tmp_path`) — no shared state between tests, no test ordering
dependencies, and no need to mock the database.

`seed.py` is a fifth, informal check: a runnable script (separate from
pytest) that walks the exact assignment scenario end-to-end and prints every
number, so the ₹12 / ₹80 / ₹68 result can be eyeballed without reading test
code.

## 12. Design decisions & trade-offs

- **SQLite with a serialized shared connection**, not Postgres or an
  in-memory dict. Zero setup for a reviewer (`pip install -r requirements.txt`
  and it just runs — no Docker, no external DB), while still getting a real
  schema with foreign keys, `CHECK` constraints, indexes, and genuine ACID
  transactions — the things an in-memory/dict-based store would only
  simulate. The lock-serialized single connection is the honest limitation:
  it caps write throughput to one transaction at a time, which is a real
  constraint SQLite has and a real one Postgres wouldn't. See §13 for how this
  maps forward.
- **Balance materialized on `wallets`, not derived by summing the ledger on
  every read.** Reads (`GET /wallet`) are O(1) instead of O(ledger size).
  Because `WalletService.apply()` writes both the new balance and the ledger
  row in the same transaction, the two can't drift apart — the ledger remains
  available as an independent way to *recompute* the balance for audit or
  disaster recovery, it's just not the hot read path.
- **Advance computed per-sale (10% of *that* sale's earning), not 10% of a
  pooled total.** Mathematically identical in aggregate, but computing it
  per-sale gives every sale a concrete `advance_paid_paise` to look up and net
  against at reconciliation — which is what makes rule #2's formula
  (`earning − advance_paid`) a simple lookup instead of a proportional
  allocation problem.
- **Negative balances allowed** (§10) — chosen over clamping-at-zero or
  raising an error, because both alternatives would either break the ledger
  invariant or make correct reconciliation fail on a legitimate business
  event.
- **Idempotency via database constraints, not just application checks.**
  `UNIQUE(sale_id)` and `UNIQUE(idempotency_key)` mean the *worst case* for a
  bug in the pre-check logic is a caught `IntegrityError`, not a corrupted
  balance. Belt-and-suspenders, but the suspenders are the ones that actually
  hold.
- **Money as integer paise, converted only at the API boundary** (§3) —
  chosen over storing `Decimal` or `float` in the database, because SQLite has
  no native decimal type and integers are the only representation with zero
  ambiguity about rounding behavior.

## 13. What I'd change for production scale

This submission optimizes for *correctness and reviewability* over
throughput. If this were going into production against real traffic:

- **Swap SQLite for Postgres**, replacing the single locked connection with a
  connection pool and `SELECT ... FOR UPDATE` (or `SERIALIZABLE` isolation) on
  the wallet row instead of a process-wide lock — the transaction boundaries
  in the service layer wouldn't need to change, only `app/database.py` and the
  handful of repository queries that use SQLite-specific syntax
  (`datetime('now')`, `ON CONFLICT`).
- **Move the advance-payout job off the request path** into a real scheduler
  (cron / Celery beat / a queue consumer) rather than exposing it as an
  on-demand `POST` — the endpoint is convenient for a demo/assignment but a
  production job shouldn't depend on someone calling an API to trigger payroll.
- **Make the withdrawal → provider status update a webhook**, not a manually
  curled endpoint — `POST /withdrawals/{id}/status` currently stands in for
  "the payment provider calls us back," which is realistic in shape but would
  need signature verification in production.
- **Partition the ledger table** (by user or by time) once it grows large
  enough that `GET /ledger` and the invariant-recomputation queries need it.

## 14. Project layout

```
app/
  money.py                       rupee⇄paise conversion, 10% advance calc
  constants.py                   SaleStatus, WithdrawalStatus, LedgerType, rule params
  exceptions.py                  DomainError hierarchy (each carries an HTTP status)
  database.py                    SQLite connection, schema DDL, transaction()/read() scopes
  repositories.py                one stateless class per table (UserRepository, SaleRepository, ...)
  services/
    wallet_service.py            the only writer of balance + ledger (WalletService.apply)
    advance_service.py           AdvancePayoutService.run() — the idempotent advance job
    reconciliation_service.py    ReconciliationService.reconcile() / reconcile_batch()
    withdrawal_service.py        WithdrawalService.request() / update_status(), 24h cooldown
  container.py                   composition root; account/catalog/summary operations
  schemas.py                     Pydantic request/response models, with_rupees() mirror
  main.py                        FastAPI app, routes, domain-error → HTTP translation
tests/
  conftest.py                    Container + FakeClock fixtures, isolated temp-file DB per test
  test_advance_payout.py         Rule 1
  test_reconciliation.py         Rule 2 (incl. both spec examples + the ₹68 worked example)
  test_withdrawal.py             Rule 3 (24h cooldown)
  test_recovery.py               Question 2 (refund on failure)
  test_invariants.py             Σ ledger == balance; negative-balance claw-back
  test_api.py                    same flows through FastAPI's TestClient
docs/
  LLD.md                         condensed design summary
  schema.sql                     standalone DDL (mirrors database.py)
seed.py                          runnable end-to-end demo, prints every number
requirements.txt
.gitignore
```

## 15. Quick start

```bash
# 1. create a virtualenv and install
python -m venv .venv
# Windows:
.\.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt

# 2. run the tests
pytest -q                 # 31 passed

# 3. see the full lifecycle print out with real numbers
python seed.py

# 4. run the API (Swagger UI at http://127.0.0.1:8000/docs)
uvicorn app.main:app --reload
```

`seed.py` output:

```
== Create 3 pending sales @ ₹40 (brand_1) ==
  pending earnings: ₹120, eligible advance: ₹12

== Run advance payout job (twice, to prove idempotency) ==
  run 1 paid 3 sales, total ₹12
  run 2 paid 0 sales (should be 0)
  wallet: ₹12

== Reconcile: reject, approve, approve ==
  wallet after reconciliation: ₹80  (expected ₹80)
  final payout beyond advance = ₹68  (expected ₹68)

== Withdraw ₹80 ==
  withdrawal #1 status=initiated wallet=₹0

== Provider FAILS the payout -> auto refund ==
  refunded=True wallet=₹80

== Ledger ==
  advance_credit                ₹4  -> balance ₹4
  advance_credit                ₹4  -> balance ₹8
  advance_credit                ₹4  -> balance ₹12
  reconciliation_adjustment     ₹-4  -> balance ₹8
  reconciliation_adjustment     ₹36  -> balance ₹44
  reconciliation_adjustment     ₹36  -> balance ₹80
  withdrawal_debit            ₹-80  -> balance ₹0
  withdrawal_refund            ₹80  -> balance ₹80
```
