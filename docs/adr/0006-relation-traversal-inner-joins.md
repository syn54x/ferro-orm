# Relation traversal always renders INNER joins; LEFT is explicit and whole-path

Relation traversal in query predicates (`lambda t: t.account.ledger_id == lid`)
always renders an **INNER** join, at every hop, regardless of whether the FK is
nullable. Keeping rows whose relation is missing requires an explicit
`.left_join(...)` chainer, which marks **every edge of its relation path** LEFT.
There is no user-facing alias surface: the relation path *is* the join identity —
the same path referenced anywhere in a query is one join; distinct paths to the
same table (two FKs, self-FKs) are distinct joins with internal aliases.

This rejects the auto-LEFT-when-nullable rule proposed by the originating
external PRD (#259, from Pinch), by owner decision (2026-07-10). The join type
only observably differs in edge cases — ordering by a related column when some
FKs are NULL, and IS-NULL-shaped predicates — and in exactly those cases
auto-LEFT is the worse behavior: making an FK nullable would silently rewrite
the semantics of existing queries, and NULL ordering diverges by dialect
(Postgres sorts NULLs last on ASC, SQLite first). For the dominant case
(`WHERE related.col = $1`) LEFT and INNER are indistinguishable, so the
"friendlier" default bought nothing. Deterministic inner semantics also make
N-hop traversal trivial to reason about — no hop's nullability poisons the
hops after it.

Conflict rules: an explicit `.left_join` beats implicit traversal on a shared
edge; explicit `.join` and explicit `.left_join` on the same edge is a
build-time error. "Has no relation" needs no join at all — `t.account == None`
desugars to an IS NULL check on the shadow FK column.

## Consequences

- Traversing a nullable relation **narrows** the result to rows where the
  relation exists. This is the documented meaning of traversal (see
  `CONTEXT.md`: Relation traversal); `.left_join` is the opt-out, not a
  schema-derived surprise.
- Changing a FK's nullability never changes the meaning of any existing query.
- A bare `.join(lambda t: t.account)` with no predicate is a meaningful
  existence filter on a nullable relation.
- Ordering by a related column drops relation-less rows unless `.left_join`
  is requested; dialect NULL-ordering divergence is therefore opt-in and
  documentable rather than default behavior.
- An `alias=` kwarg can be added later without breaking anything; the reverse
  (removing a shipped alias surface) would not be true. Same-relation-twice
  joins are meaningless for filter/sort, so no expressiveness is lost.
