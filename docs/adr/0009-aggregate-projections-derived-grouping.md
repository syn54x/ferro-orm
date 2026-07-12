# Aggregate projections: grouping derived from the projection, pinned decode contract, flat record+relation results

Aggregation rides the `record` materialization plan (ADR-0007): aggregate
fields (`t.amount.sum()` — methods on scalar field proxies; the closed set
`count/sum/avg/min/max`) land in projected records, and a projection
containing any aggregate is an **aggregate projection** (`CONTEXT.md`) in
which every non-aggregate field is a group key — GROUP BY is **derived from
the projection, never declared**; there is no `group_by()` chainer. Aggregate
result types are a **pinned cross-backend contract** derived from the source
column's Python type (`count → int`; `min`/`max` → source type and codec;
`sum` → source numeric type; `avg` → float for int/float, Decimal for
Decimal); source families with no portable cross-backend meaning (enum, uuid,
json, bool) are rejected at build time. Record results that reach across a
relation are **flat** — traversed projection with output aliases — and
`include()` × `select()` is rejected permanently (flattened-as-final),
closing the question ADR-0008 parked with #282.

Decision by owner (2026-07-12), grilling #282 on the partial-materialization
substrate (#277/#279).

Rejected alternatives:

- **An explicit `group_by()` chainer**: SQL's own rule already forces every
  bare selected column to be a group key, so a chainer is pure redundancy
  plus a new error class ("bare column not in group_by") that Postgres
  rejects at runtime and SQLite answers with an *arbitrary row's value*.
  Derivation makes the invalid query unwritable and keeps the dict literal
  the whole story: the record shape determines the grouping. The only
  expressiveness lost — grouping by an unselected column — can arrive later
  additively; the reverse (retiring a shipped chainer) could not.
- **Backend-native result types** (driver passthrough): Postgres `SUM(int8)`
  and `AVG(int)` are `numeric`; SQLite `sum(int)` is int and `avg` is float —
  the same query would return different Python types per backend.
- **Aggregating any column family, letting the DB decide**: `min`/`max` over
  uuid does not exist on Postgres, and over a native enum silently diverges
  (definition order vs. SQLite's lexical text order). Where no portable
  meaning exists, build time is the only honest place to fail.
- **Nested record+relation results** (`Row(amount=…, account=<Account>)`): a
  third result shape — part record, part instance — needing its own decode,
  typing, and identity story, while every concrete need is served flatter and
  clearer by a traversed, aliased field. Records carry values; instances
  carry rows.
- **Coalescing empty aggregates to zero**: "sum of no rows" and "sum of rows
  totaling zero" are different facts, and zero is the wrong identity for
  `min`/`max` anyway. SQL's NULL passes through as `None` uniformly
  (`count → 0`), with no hidden COALESCE.

## Consequences

- The canonical grouped query is one lambda —
  `select(lambda t: {"acct": t.account_id, "total": t.amount.sum()})` renders
  `GROUP BY account_id`. Aggregate-only projections collapse to one record
  (read with `first()`); a grouped query over zero rows returns zero records.
- GROUP BY never travels on the wire: the renderer derives the keys from the
  v5 `record` plan (every non-`expr` field), pinned by golden vectors —
  renderer work, like joins rendering from paths.
- Aggregate record fields are `T | None` (`count` excepted) — empty-input
  NULL passes through.
- `order_by` strings resolve output field names before root columns on a
  projected query (SQL's own ORDER BY scoping); on an aggregate projection
  every sort key must be a group key or an aggregate — build-time error, the
  SQLite arbitrary-row trap made unwritable. `limit()`/`offset()` act on
  groups.
- `count()`/`exists()` raise on an aggregate projection with guidance (rows:
  unprojected query; groups: `len(await q.all())`) — #279's "unaffected by
  projection" contract is the *plain*-projection rule.
- `where()` never accepts an aggregate predicate; post-aggregation filtering
  is `having()` (#291).
- A left-joined traversed group key yields a `None`-keyed group — "has no
  relation" is a visible bucket, not a dropped row.
