# Populated relations: include() is the data axis; populated access is plain attribute access

A query opts into joined-row hydration with an explicit `.include(lambda t:
t.account.owner)` (`CONTEXT.md`: Include, Populated relation). A populated
forward-FK relation is accessed as a plain attribute — `txn.account` *is* the
complete related instance, matching the field's declared annotation — while an
unpopulated relation keeps today's awaitable contract unchanged. Awaiting a
populated relation is a hard break (documented), and there is no separate
"loaded" model type: `.all()` on an included query still returns `list[T]`.

Include is the third orthogonal axis of a query: joins decide membership,
projection decides shape, include decides attached data. An include never
changes which rows come back — it contributes no join-type opinion on any edge
another clause references, and renders LEFT joins only for edges nothing else
touches. Its result rides the `instances` materialization plan (ADR-0007),
whose wire shape carries relation paths as hop facts, separate from the
`joins` section; the envelope bumps to v4 when it first ships (one-wheel
policy, #269/#278 precedent).

Decision by owner (2026-07-11), designing #267 stage 2 on the
partial-materialization substrate (#277). Scope is forward-FK paths at any
depth; reverse (BackRef) and M2M population are a future separate mechanism
(batched second query), not `include`.

Rejected alternatives:

- **Await-always, cache-resolving access** (`await txn.account` resolves from
  the populated cache): zero access-shape maybes, but no async ORM does it,
  story 29's ergonomics never materialize, and the `account: Account`
  annotation stays a permanent static lie. Population should change *cost and
  data*, and (A) additionally makes the declared type honest exactly when the
  user opted in.
- **Awaitable model instances** (`Model.__await__` returning `self` so old
  `await` code survives population): `await` stops signaling I/O on every
  model everywhere, masking real bugs, to smooth one migration edge.
- **A distinct static result type** (`Loaded[Transaction]`): inexpressible
  honestly today (the base annotation already claims the instance; per-field
  transformation needs PEP 827), and unsound under the identity map — the
  same object is shared with plain queries in a session, so a per-query type
  brand asserts what the runtime cannot pin. When PEP 827 lands, typing can
  sharpen with zero runtime change, precisely because the runtime type stayed
  single.
- **Include implies INNER / rides the `joins` section** (Oxyde-style
  `join("author")` conflation): asking for *data* would narrow *membership*
  (nullable FKs vanish), and a whole-path LEFT entry on the wire would lift
  predicate-shared edges to LEFT, silently changing stage-1 predicate
  semantics on shared paths.
- **Query-local dedup for sessionless includes**: a third identity regime
  between "identity map" and "fresh instances everywhere". Sessionless stays
  fresh-per-row; the session is the one answer to "same row, same object".

## Consequences

- Populated instances take the full identity-map protocol per hop — map hit →
  refresh + reuse, miss → hydrate + insert — safe because every included hop
  selects the complete row (`hop.*`, the complete-instance invariant at every
  node; per-hop decode uses the hop model's own codec plan).
- Refresh keeps a population iff it is still true: if a refreshed shadow FK
  still equals the populated instance's pk, the population survives (so
  populations accumulate across queries in a session); if the FK changed or
  went NULL it is dropped — access reverts to the awaitable, never silently
  wrong, and never "repaired" from whatever the map happens to hold.
- Including a path populates every hop along it; shared prefixes dedup by
  path identity; a NULL mid-chain ends the chain as populated-`None` (the
  declared `| None` type), with the root row retained.
- One materialization plan per query stays the law: `include()` on a
  projected query (and `select(columns…)` on an included one) raises at build
  time pointing at #282, which owns the flattened-vs-nested design for
  record+relation results. Mutations reject include; `count()`/`exists()`
  are unaffected (their payloads emit `root_instances`, no include joins
  rendered).
- `include()` is a Query chainer only (no classmethod), lambda-selector only
  (strings never traverse, #280 precedent); selectors naming BackRef/M2M
  relations raise naming the future mechanism.
