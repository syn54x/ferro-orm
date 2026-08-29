# Roadmap

Ferro is pre-1.0 and under active development. The items below are known gaps we intend to close; priorities are driven by what users actually hit, so [issues](https://github.com/syn54x/ferro-orm/issues) move things up this list.

## Query Features

- **`having()`** — post-aggregation filtering for [grouped queries](guide/aggregations.md); `where()` rejects aggregate predicates pointing at it ([#291](https://github.com/syn54x/ferro-orm/issues/291)). Workaround: filter groups in Python after `all()`.
- **Typed record fields** — a projected `Row`'s fields type as `Any` on access today; inferring per-field types from the selector (so `row.total` checks as `int | None`) is [#290](https://github.com/syn54x/ferro-orm/issues/290).
- **Reverse and many-to-many population** — [`include()`](guide/queries.md#populating-relations-with-include) populates forward-FK paths in one statement; populating `BackRef` collections and M2M sets is a separate future mechanism (a batched second query stitched onto the results). Today each awaited collection is its own query. (*Filtering* on reverse/M2M membership already works — the [existence test](guide/queries.md#existence-tests-on-reverse-many-to-many-relations), `t.lines.exists(...)`.)
- **Cross-scope correlation in existence tests** — comparing an inner-lambda column against the outer scope (`t.lines.exists(lambda line: line.category_id == t.category_id)`) is rejected at build time today; correlated column-to-column comparison is [#309](https://github.com/syn54x/ferro-orm/issues/309).
- **`ilike()`** — case-insensitive pattern matching. Workaround: `like()` with normalized case.
- **Richer update recipes** — `+` / `-` and `now` already run in the database (`update(lambda counter: {"n": counter.n + 1, "updated_at": now})`). `.merge()` / `.concat()` are [#379](https://github.com/syn54x/ferro-orm/issues/379); other operators (`*`, `/`) are still load–modify–`save()` or [raw SQL](guide/raw-sql.md).

## Connections

- **`disconnect()`** — graceful pool shutdown for application shutdown hooks. Today cleanup happens at process exit.
- **Health checks** — a `check_connection()`-style probe for readiness endpoints. Workaround: run a trivial query and catch the failure.
- **Richer pool configuration** — `PoolConfig` covers `max_connections`/`min_connections` today; acquire timeouts, idle timeouts, and max connection lifetime are future work.

## Influencing Priorities

None of this is on a promised schedule. If one of these gaps blocks you, say so on the [issue tracker](https://github.com/syn54x/ferro-orm/issues) — a concrete use case is the strongest signal we get, and [contributions](contributing.md) are welcome.
