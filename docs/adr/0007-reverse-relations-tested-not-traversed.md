# Reverse relations are tested, not traversed

A reverse (BackRef) or many-to-many relation appears in a query predicate in
exactly one form: the **existence test** — `t.lines.exists(...)`, optionally
scoped by an inner predicate over the related model, negated with `~`. It
renders as a correlated `EXISTS` subquery at every cardinality (one-to-one
BackRefs included) and for every relation kind (reverse FK and M2M, the latter
correlating through the join table). The result stays root-shaped: an
existence test never multiplies root rows and composes with any other
predicate, ordering, and paging.

The proxy for a reverse relation exposes `.exists()` and nothing else. Column
access (`t.lines.category_id`), comparison (`t.transfer_out != None`), and
`in_` all raise at build time with the supported spelling in the message.
There is deliberately no implicit path traversal onto reverse edges — the
spelling sketched by the originating external PRDs (#307/#308, from Pinch).
Implicit traversal has an unanswerable grouping ambiguity: in
`(t.lines.a == 1) & (t.lines.b == 2)` nothing visible says whether one child
row must match both conditions or some child may match each. Django chose
implicit and the resulting confusion is a documented hazard of that API. The
explicit combinator makes grouping the user's choice —
`t.lines.exists(lambda l: A & B)` versus
`t.lines.exists(A-test) & t.lines.exists(B-test)` — both spellable, both
unambiguous. This is the same determinism-over-friendliness ground on which
ADR-0006 rejected auto-LEFT.

The inner predicate is a full ferro predicate over the related model: same
operators, forward traversal (rendered as joins inside the subquery, ADR-0006
semantics unchanged), and nested existence tests to arbitrary depth. What it
may not do (v1) is reference any scope other than its own parameter —
cross-scope column comparison is rejected at build time and tracked as a
possible future extension.

Rejected alternatives:

- **`.any()`/`.has()` cardinality split (SQLAlchemy)** — two names for one
  mechanism, and call sites break when a FK's `unique=` changes. One concept,
  one verb: `.exists()` at every cardinality.
- **`in_(single-column ProjectedQuery)`** — declined, not deferred. The
  workloads it serves are existence-test workloads; it forces callers to
  hand-correlate on id columns and leaks the join column into every call site.
- **LEFT JOIN + IS NOT NULL specialization for unique BackRefs** — safe but a
  second render path with no user-visible benefit; planners already rewrite
  correlated EXISTS into semi-joins. May become an invisible planner-side
  optimization later if measured.
- **`!= None` / `== None` sugar for bare existence** — forward `== None`
  desugars to IS NULL on a shadow column that physically lives on the root
  table (ADR-0006); a reverse relation has no root-side column, so the sugar
  would be silent implicit traversal.

## Consequences

- The glossary term "Relation traversal" stays forward-FK-only, and that
  narrowness is now load-bearing rather than incidental.
- `include()` (population) and `.exists()` (membership) remain distinct axes;
  BackRef population is still a separate future mechanism.
- The wire IR gains one recursive node kind for the existence test, carrying a
  correlation hop path (1 hop for reverse FK, 2 for M2M) — the Rust renderer
  handles both with one loop, so M2M support is test surface, not mechanism.
- `left_join()` on a reverse edge stays rejected (it would reopen the
  "join never multiplies root rows" property) but the error now names
  `.exists()` as the supported spelling.
