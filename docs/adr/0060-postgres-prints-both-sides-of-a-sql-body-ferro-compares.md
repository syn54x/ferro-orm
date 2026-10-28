# Postgres prints both sides of a SQL body Ferro compares

```
declared:        sign("amount_minor")
catalog:         sign((amount_minor)::double precision)
EXPLAIN prints:  sign((amount_minor)::double precision)   for both  → no drift
```

On Postgres, whether a live check or generated expression drifted is decided by asking Postgres to print both sides: one `EXPLAIN (VERBOSE, FORMAT JSON) SELECT <declared rendering>, <catalog reprint> FROM <live table>` inside a read-only transaction. Both pass through the same analysis, which adds the same implicit casts and folds the same constants; equal outputs are no drift. Nothing is created, so it runs under a read-only role and against a replica. It needs `SELECT` on the table, and a role without it is refused naming the `GRANT`. A comparison only ever happens against a live database, which already holds the connection; `ferro migrate new` compares two snapshots and never needs it. SQLite keeps the text comparison, since it stores the body as written.

Once a body can call a function, Postgres adds casts that depend on its catalog (`sign(bigint)` resolves to `sign(double precision)`), and a hand-written normalizer would need a rule for every function and argument type, each a #563-style bug hunt. Views (#789 epic 2) reuse the same comparison.

Decision by owner (2026-10-28), grilling #789. Supersedes ADR-0015 on Postgres.

Rejected alternatives:

- **Keep the hand-written normalizer** (ADR-0015, #563): reports drift on `abs(amount) > sign(amount)`, a check Ferro created itself.
- **A temp table in a rolled-back transaction**: fails under a read-only role or on a replica (`cannot execute CREATE TABLE in a read-only transaction`).
- **Predict the reprint from the function table**: a second renderer copying Postgres's printer per major version; a new release would show every user phantom drift.
- **`libpg_query`**: raw parsing only, no catalog analysis, so no casts.
- **An embedded Postgres (PGlite)**: must match the server's major version; a heavy dependency.
