# Table-check body drift is a canonical render compared through one normalizer

Whether a live table check's predicate drifted is decided by one comparison: ferro's canonical CHECK body (one renderer, same seam as column-check `render_check_body`) versus `pg_get_constraintdef`, both run through one normalizer (whitespace, wrapping parens, ident quotes, and the grouping of one associative connective). Differ → constraint rebuild; equal → no-op. Tests pin real `pg_get_constraintdef` output for the boolean/`IS NULL` shapes so a quoting change fails the suite instead of emitting phantom drop+add. Alembic autogenerate consumes the same comparison over FFI.

Amended 2026-09-28 (#437): a chain of one connective compares flat. ferro renders `a | b | c` left-nested, `((a) OR (b)) OR (c)`; Postgres parses that into one n-ary `BoolExpr` and `pg_get_constraintdef` prints `(a) OR (b) OR (c)`. Same predicate, so the normalizer splices a parenthesized operand into the chain around it when both use the same connective (`flatten_associative_chains`). `AND` inside `OR` (and the reverse), a `NOT` operand, and a function argument keep their parentheses: that grouping is the predicate. Pinned with real `pg_get_constraintdef` output for three- and four-term `OR` and `AND` chains, right-nested chains, and mixed shapes.

Decision by owner (2026-08-18), grilling #339.

Rejected alternatives:

- **Name-only identity**: changing the lambda and keeping the suffix would leave the old body enforcing — silence about definition drift.
- **`COMMENT ON CONSTRAINT` hashes**: a second ownership channel next to the `ck_*` prefix; comments are not the schema object.
- **Rebuild every connect**: exclusive lock and revalidation of existing rows for a no-op.
- **Render the chain flat instead of normalizing it** (#437): would fix only the shape ferro itself emits going forward. Constraints already live, a right-nested lambda (`a | (b | c)`, which Postgres keeps nested), and SQLite's verbatim `CHECK (…)` text would still differ. The comparison is the normalizer's job; the renderer stays a plain tree walk.
