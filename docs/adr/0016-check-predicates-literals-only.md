# Check predicates are root-column lambdas with inlined literals

A check predicate is the `where()` dialect restricted to this table: comparisons, `IS NULL`, AND/OR/NOT, `.in_()`, `.like()`, column-to-column. Values are literals (including enum members), inlined into the CHECK body — a CHECK cannot carry bind parameters. A closed-over variable or call fails at class definition. Forward-FK null tests accept both spellings — `t.outflow_transaction == None` (join-free shadow-FK leaf) and `t.outflow_transaction_id == None` — compiled at resolved registration so the shadow column exists. Traversal (`t.outflow_transaction.amount`), existence tests, and aggregates stay rejected; SQL functions wait for the predicate dialect to grow (ADR-0012).

Decision by owner (2026-08-18), grilling #339.

Rejected alternatives:

- **Transfer-only `IS NULL` / `OR`**: covers Pinch's four CHECKs and nothing else; the next `amount >= 0` would need a follow-up dialect.
- **Snapshot closed-over variables**: `lambda t: t.amount > min_amount` would bake import-time state into schema, looking like config and drifting across processes.
- **`_id` only or relation-null only**: one spelling fights I-8, the other fights the PRD and anyone who thinks in columns.

