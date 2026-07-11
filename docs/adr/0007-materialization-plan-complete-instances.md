# Model instances are never partial; projection returns records via an explicit materialization plan

A model instance always carries a complete row (`CONTEXT.md`: Complete-instance
invariant). Selecting a subset of columns — or an aggregate that belongs to no
model — returns a **projected record** (`Row`, in the list-like `Rows`
container), never a partial model instance and never a lazily-completed one.
What a query's columns become is declared by an explicit **materialization
plan** carried in the QueryIR payload as data (`root_instances` today,
`record` for projections, an `instances` graph kind reserved for joined-row
hydration), not inferred from the shape of a column list.

Decision by owner (2026-07-10), designing the shared partial-materialization
mechanism that joined-row hydration (#267 stage 2), partial selects, and
aggregations all consume.

Rejected alternatives:

- **Partial model instances** (Django `.only()` style): every `Transaction`
  becomes a *maybe* — `save()`, refresh, the identity map, and any helper
  taking a model must know which query produced their argument; unselected
  fields fail at attribute-touch time, far from the query that caused it.
- **Deferred fields** (lazy per-field loads): hidden N+1 — the disease the
  joins epic exists to cure — and structurally hostile in async Python, where
  a plain attribute access cannot await.
- **Bare `select` column list in the IR** (plan by convention): a column list
  cannot say "these columns are an Account; attach it to `t.account`", so
  stage 2 would need a second payload concept and the v1 shape would be
  churned or orphaned. The plan-as-data section grows by adding kinds and
  field shapes — additive, like the v2 `joins` section was.

## Consequences

- `Row`/`Rows` are pydantic-shaped (FastAPI `response_model`, `model_dump()`
  for free) but never pydantic-constructed: instances come off the same
  direct-to-dict hydration path as models (AGENTS.md I-2), and `Rows` wraps
  without re-validating. Projection can never be slower per row than full
  hydration.
- Projected records carry no persistence identity: no `save()`, no refresh,
  no identity-map participation.
- `select()` overloads: bare = full query (unchanged); a lambda selector or
  column-name strings = projection, validated at build time like `order_by`'s
  forms; string paths and mixed string/lambda calls are rejected loudly.
- `update()`/`delete()` on a projected query raise at build time (a projection
  is a read shape; silently ignoring it would make `select(...)` a no-op on
  mutations). A second `select(...)` on the same query raises: replacing a
  projection changes the result type mid-chain.
- Record field names are declared separately from source columns in the plan,
  fields carry a relation path, and a field may later carry an expression
  instead of a column — so output aliases, traversed projection, and
  aggregations slot in without reshaping the v1 contract. `instances` is a
  declared-but-rejected kind until stage 2 builds it.
- The QueryIR envelope bumps to a new single supported version with the new
  payload section, per the one-wheel policy established with v2.
