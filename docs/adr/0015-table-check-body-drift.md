# Table-check body drift is a canonical render compared through one normalizer

Whether a live table check's predicate drifted is decided by one comparison: ferro's canonical CHECK body (one renderer, same seam as column-check `render_check_body`) versus `pg_get_constraintdef`, both run through one normalizer (whitespace, wrapping parens, ident quotes). Differ → constraint rebuild; equal → no-op. Tests pin real `pg_get_constraintdef` output for the boolean/`IS NULL` shapes so a quoting change fails the suite instead of emitting phantom drop+add. Alembic autogenerate consumes the same comparison over FFI.

Decision by owner (2026-08-18), grilling #339.

Rejected alternatives:

- **Name-only identity**: changing the lambda and keeping the suffix would leave the old body enforcing — silence about definition drift.
- **`COMMENT ON CONSTRAINT` hashes**: a second ownership channel next to the `ck_*` prefix; comments are not the schema object.
- **Rebuild every connect**: exclusive lock and revalidation of existing rows for a no-op.
