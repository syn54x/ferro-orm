# Roadmap

Ferro is pre-1.0 and under active development. The items below are known gaps we intend to close; priorities are driven by what users actually hit, so [issues](https://github.com/syn54x/ferro-orm/issues) move things up this list.

## Query Features

- **Aggregations beyond `count()`/`exists()`** — `sum`, `avg`, `min`, `max` on the query builder, plus output aliases and traversed projection (`select(lambda t: t.account.name)`), designed together on the [partial-select](guide/queries.md#selecting-a-column-subset) substrate. Today you either compute in Python after fetching or drop to raw SQL.
- **Reverse and many-to-many population** — [`include()`](guide/queries.md#populating-relations-with-include) populates forward-FK paths in one statement; populating `BackRef` collections and M2M sets is a separate future mechanism (a batched second query stitched onto the results). Today each awaited collection is its own query.
- **`ilike()`** — case-insensitive pattern matching. Workaround: `like()` with normalized case.
- **`not_in_()`** — NOT IN exclusion lists. Workaround: combine `!=` comparisons with `&`.
- **Atomic update expressions** — database-side expressions in batch updates, e.g. `update(view_count=Post.view_count + 1)`, avoiding the read-modify-write race. Workaround today: load, mutate, `save()` (or raw SQL).

## Connections

- **`disconnect()`** — graceful pool shutdown for application shutdown hooks. Today cleanup happens at process exit.
- **Health checks** — a `check_connection()`-style probe for readiness endpoints. Workaround: run a trivial query and catch the failure.
- **Richer pool configuration** — `PoolConfig` covers `max_connections`/`min_connections` today; acquire timeouts, idle timeouts, and max connection lifetime are future work.

## Influencing Priorities

None of this is on a promised schedule. If one of these gaps blocks you, say so on the [issue tracker](https://github.com/syn54x/ferro-orm/issues) — a concrete use case is the strongest signal we get, and [contributions](contributing.md) are welcome.
