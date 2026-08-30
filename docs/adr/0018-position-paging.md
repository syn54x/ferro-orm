# `after`/`before` are position paging, not a derived predicate

`Query.after(position)` / `Query.before(position)` are a **paging start** —
a sibling of `limit`/`offset` on the QueryIR payload — not a `where()`
predicate the query writes for itself. A position is the ordered tuple of
the query's order-key values; `position_of(row)` reads it; `after(row)` is
sugar. Cursor encoding is the caller's.

The bound is exclusive. `after` + `offset`, or `after` + `before`, is a
build-time error (`count()` already drops paging). Order keys are root or
traversed columns (not aggregates); they must include the model's primary
key so two rows never share a position. `None` is legal in every non-PK
slot — column nullability is the wrong question, because a `left_join`'d
NOT NULL related column is still NULL when the relation is missing.

`before(position).limit(n)` is the **adjacent previous page**, yielded in
the declared order (flip comparisons and order, fetch n, reverse).
Unbounded `before()` is every earlier row in declared order — a prefix,
not a page. Limit is optional on both sides; on unbounded `before()`,
`first()` (limit 1 → adjacent) and `all()[0]` (prefix head) disagree.
That is accepted and documented.

Same chainers on `ProjectedQuery`. `position_of(Row)` requires every order
key to be in the projection; otherwise pass a tuple. Grouped aggregates
fail the PK-in-order-keys rule.

Rejected: injecting the keyset tree into `where` (makes predicates depend
on `order_by`); an opaque Position type (Pinch must rebuild from a decoded
cursor); silently appending the PK (hidden extra sort); requiring `nulls=`
only when paging (ADR-0017).

See `CONTEXT.md`: Paging, Position, Order key. Grilled with #372.
