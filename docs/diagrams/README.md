# Flow diagrams (Graphviz)

Eight `.dot` files tracing the system's structure and behavior, each named to
match the section it documents in the main [README](../../README.md) and
[LLD](../LLD.md):

| File | Shows |
|---|---|
| `01_architecture.dot` | The four-layer architecture (HTTP → composition root → services → data access) |
| `02_er_diagram.dot` | Full entity-relationship diagram of the schema |
| `03_sale_lifecycle.dot` | Sale status state machine (`pending → approved/rejected`) with the advance job as a self-loop |
| `04_withdrawal_lifecycle.dot` | Withdrawal status state machine, including the refund-on-failure transitions |
| `05_advance_payout_flow.dot` | `AdvancePayoutService.run()` step by step, including the idempotency guard |
| `06_reconciliation_flow.dot` | `ReconciliationService.reconcile()` step by step, including the delta formula branch |
| `07_withdrawal_request_flow.dot` | `WithdrawalService.request()`, including the idempotency-key short-circuit and the 24h cooldown check |
| `08_withdrawal_status_update_flow.dot` | `WithdrawalService.update_status()`, including the terminal-state guard and refund branch |

## Rendering

Any Graphviz-compatible tool will render these. A few options:

```bash
# Graphviz CLI (brew install graphviz / apt install graphviz / choco install graphviz)
dot -Tsvg docs/diagrams/03_sale_lifecycle.dot -o sale_lifecycle.svg
dot -Tpng docs/diagrams/06_reconciliation_flow.dot -o reconciliation_flow.png

# render all of them at once
for f in docs/diagrams/*.dot; do dot -Tsvg "$f" -o "${f%.dot}.svg"; done
```

Editor options: the "Graphviz Interactive Preview" or "Graphviz (dot) language
support" extensions in VS Code render these live as you edit, with no CLI
install needed.

A live-rendered (Mermaid) version of the same eight diagrams, with the DOT
source alongside each one for copy-paste, is available as a published
Artifact from the conversation that generated this project.
