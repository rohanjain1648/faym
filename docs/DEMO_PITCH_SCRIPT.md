# 🎙️ Faym — 5 to 7 Minute Live Demo & Pitch Script

A structured presentation and live demonstration guide for the **Faym User Payout Management System**.

---

## 📌 Presentation Overview

| Metric | Details |
| :--- | :--- |
| **Total Duration** | 5 – 7 Minutes |
| **Target Audience** | Engineering Leads, System Architects, Product Managers |
| **Key Objective** | Demonstrate mathematical correctness, zero float-drift, clean architecture, idempotency, and resilient refund handling under real-world payout conditions. |
| **Prerequisites** | Terminal open at repo root (`python seed.py` ready or server running on `http://localhost:8000`). |

---

## 🕒 Minute-by-Minute Pitch Script

### Phase 1: Hook & Problem Statement (0:00 - 1:00)

**[Speaker Action]**: Project cover slide or terminal open.

> **"Hello everyone. Today I'm presenting the User Payout Management System — Faym.**
>
> In affiliate marketing and creator monetization platforms, payouts are inherently complex. Sales don't settle instantly; they start as **pending**.
>
> To improve user retention, platforms often issue an **advance payout** (e.g. 10% of pending earnings) before a sale is reconciled. Later, an admin manually reconciles the sale to **approved** (customer kept product) or **rejected** (refunded/returned).
>
> The central challenge is **financial convergence**: How do you guarantee that regardless of out-of-order execution—whether advances run 5 times before reconciliation, or reconciliation happens immediately—the user's withdrawable balance *always* matches their true earned income, without money leaks or float discrepancies?"

---

### Phase 2: The Core Financial Model (1:00 - 2:00)

**[Speaker Action]**: Show the Event Delta Table or `docs/LLD.md`.

> **"Rather than recalculating balance on the fly with custom logic for every edge case, Faym treats the wallet as a state machine driven by signed ledger events:**
>
> - **Advance Paid**: `+ 10% × earning`
> - **Reconciled Approved**: `+ (earning − advance_paid)`
> - **Reconciled Rejected**: `− advance_paid`
>
> Notice how this mathematically converges:
> - For an **Approved sale**: `+advance + (earning - advance) = +earning`.
> - For a **Rejected sale**: `+advance - advance = 0`.
>
> It doesn't matter if an advance was paid before or after reconciliation. If no advance was paid when approved, `advance_paid` is 0, so `+ (earning - 0) = +earning`. It is self-correcting and elegant."

---

### Phase 3: Live Terminal Demonstration (2:00 - 4:00)

**[Speaker Action]**: Run `python seed.py` in the terminal. Point out console outputs as they appear.

```bash
python seed.py
```

> **"Let me show you this in action using our reference scenario from the seed script:**
>
> 1. **Pending Sales**: We create 3 pending sales of **₹40** each for user `john_doe`. Pending earnings equal **₹120**.
> 2. **Advance Payout & Idempotency**:
>    - We execute the advance payout job. Each sale yields a 10% advance (₹4 × 3 = **₹12** added to wallet).
>    - We immediately trigger the advance job a *second time*. Notice `sales_paid: 0`. Database-level `UNIQUE(sale_id)` constraints enforce structural idempotency—it's impossible to double-pay.
> 3. **Reconciliation**:
>    - Admin rejects Sale #1: balance drops by ₹4 (claw-back of advance).
>    - Admin approves Sale #2 & Sale #3: balance gains `+(40 - 4) = +36` for each.
>    - **Resulting Wallet Balance**: **₹80** (`40 + 40`).
>    - **Net Final Payout (beyond advance)**: `80 - 12 =` **₹68**, matching the assignment specification precisely.
> 4. **Withdrawals & 24h Cooldown**:
>    - `john_doe` requests a withdrawal of **₹80**. Wallet drops to **₹0**.
>    - If the user attempts another withdrawal immediately, the system rejects it with a `429 Cooldown Error` enforcing a single withdrawal per 24-hour window.
> 5. **Provider Failure & Auto-Refund**:
>    - The payment gateway reports a failure (e.g., bank timeout).
>    - The engine automatically refunds **₹80** back to the wallet, resets the 24h clock, and logs the event to the ledger so the user can re-attempt withdrawal."

---

### Phase 4: Financial Precision & Architecture (4:00 - 5:30)

**[Speaker Action]**: Highlight project structure / `app/money.py` and `app/services/wallet_service.py`.

> **"Under the hood, Faym achieves enterprise-grade financial safety through three core design decisions:**
>
> 1. **Integer Paise Precision**:
>    - Floating-point arithmetic (`0.1 + 0.2 = 0.30000000000000004`) causes sub-paisa leakage in financial systems.
>    - All amounts inside Faym are stored as `INTEGER` paise (1 Rupee = 100 Paise). Conversions at the API boundary use `Decimal` with explicit `ROUND_HALF_UP`.
>
> 2. **Single Choke-Point Balance Mutation**:
>    - All balance modifications flow exclusively through `WalletService.apply()`.
>    - In a single SQL transaction (`BEGIN IMMEDIATE`), it updates the wallet balance and appends a structured row to the `ledger_entries` audit log.
>
> 3. **4-Layer Clean Architecture**:
>    - **FastAPI HTTP Layer** handles Pydantic validation & exception translation.
>    - **Composition Root (`Container`)** wires dependencies cleanly.
>    - **Domain Services** encapsulate business rules without HTTP/Database coupling.
>    - **Repositories** manage SQL parameters."

---

### Phase 5: Auditability & Wrap-Up (5:30 - 7:00)

**[Speaker Action]**: Show the Ledger Output from `seed.py`.

> **"Finally, look at the append-only audit ledger output:**
>
> ```text
> advance_credit                ₹4  -> balance ₹4
> advance_credit                ₹4  -> balance ₹8
> advance_credit                ₹4  -> balance ₹12
> reconciliation_adjustment    -₹4  -> balance ₹8
> reconciliation_adjustment     ₹36  -> balance ₹44
> reconciliation_adjustment     ₹36  -> balance ₹80
> withdrawal_debit            -₹80  -> balance ₹0
> withdrawal_refund             ₹80  -> balance ₹80
> ```
>
> We enforce an absolute invariant: `SUM(ledger_entries.amount_paise) == wallet.withdrawable_balance_paise`. The balance is not just a live integer—it is a completely auditable state derived from immutable financial events.
>
> In production, replacing SQLite with PostgreSQL requires changing only `database.py` while the entire core service layer and domain logic remain untouched.
>
> Thank you! I am now happy to take any questions."

---

## 💡 Speaker Q&A Quick-Reference Guide

| Potential Question | Recommended Response |
| :--- | :--- |
| **Q: What happens if a sale is rejected after the user already withdrew the advance?** | *"The claw-back (`-advance_paid`) applies to the wallet. If balance is 0, the balance goes negative (debt). Future approved earnings automatically pay down the debt before withdrawal is allowed again."* |
| **Q: How does the system handle high-concurrency balance updates?** | *"All operations run in `BEGIN IMMEDIATE` transactions. In SQLite, access is serialized. In PostgreSQL, `SELECT ... FOR UPDATE` on the user's `wallets` row prevents race conditions."* |
| **Q: How is idempotency guaranteed if an HTTP client retries a withdrawal?** | *"Clients pass a unique `idempotency_key`. The `withdrawals` table enforces `UNIQUE(idempotency_key)`. If retried, the API returns the original response without double-debiting."* |
