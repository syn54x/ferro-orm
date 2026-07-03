# FF-D — Identity map & routing redesign (design)

**Status:** approved (design review 2026-07-02)
**Epic:** FF-D (`docs/plans/2026-07-02-001-fable-fixes-roadmap.md` L235–281)
**Findings closed:** F3 (unbounded strong-ref identity map, stale reads,
global-clear invalidation), F13 (stringly-typed route triple resolved twice;
`using=` silently bypasses ambient session; `Model.__init__` reads target PK
by the source model's PK name).
**Ships in:** v0.13, one PR, no deprecation staging. This lands the
ambient-default-connection removal one minor ahead of the shipped v0.14
deprecation notice — deliberate, accepted, and stated in the PR.

**Objective:** identity is session-scoped, memory-bounded, and never returns
stale data; routing is resolved exactly once through one handle.

---

## Central decision — the sessionless path (Option B)

Sessions are never created implicitly. Today, an operation with
`session_id=None` routes identity through the process-global `IDENTITY_MAP`
(`src/state.rs:212`) — one strong-ref map shared by every sessionless op for
the life of the process. That map is F3.

**Decision: sessionless operations still run, but with no identity map.**

- **Session-bound** (ambient `async with ferro.engines.session(...)` or
  explicit `session=`): full identity semantics — dedup (`a is b`),
  weak-value map, refresh-on-load — in `SessionState.identity_map`.
- **Sessionless with explicit route** (`using="name"`, no session): runs
  normally against that connection. No identity map: no dedup, no cache,
  therefore no stale-read surface. Identity primitives' `None` arm becomes a
  no-op (get → miss, insert → nothing).
- **No route at all** (no session, no `using`): **error.** The
  legacy default-connection path (deprecated since v0.12, removal announced
  for v0.14) is removed now. Structurally unrepresentable — see RouteHandle.

Rejected alternatives:

- **A — sessions mandatory:** breaks legitimate, non-deprecated
  `using=`-only code; the v0.14 notice never announced universal sessions.
- **C — implicit per-operation session:** a per-op session's map dies with
  the op, so it never dedupes across calls — behaviorally identical to B
  plus a `register_session`/`close_session` round-trip per query. Giving the
  implicit session a longer lifetime has no natural boundary and
  reintroduces F3.

Identity is a feature you opt into by naming a scope, because the scope is
the only honest bound on the map's lifetime.

**User-observable breakage (v0.13):**

1. No-session, no-`using` operations raise instead of warning (was: v0.14).
2. Explicit `using=` conflicting with the ambient session raises
   `ValueError` instead of silently running sessionless (D4).
3. `using=`-only code loses cross-call `a is b` (the data is fresh; only
   object identity is gone — wrap in a session to get it back).

---

## D1a — Weak-value identity map

**Representation.** Map value changes from a strong `Py<PyAny>` (the
instance) to a `Py<PyAny>` holding a **`weakref.ref(instance)`** created at
insert time via PyO3's weakref API. Key shape unchanged:
`(connection_name, model_name, pk_string)`. Lives only on
`SessionState.identity_map` (`src/state.rs:225`); the global map is deleted
(D2). The map owns the weakref object strongly; the instance is kept alive
only by user code.

- **get:** fetch entry → **upgrade** (call the weakref) → live strong ref =
  hit; dead referent = remove tombstone, report miss. Hit-vs-miss is decided
  on the upgraded strong ref, never the raw entry, so the instance cannot
  die between "hit" and "use".
- **insert:** create the weakref; if the class cannot be weakly referenced,
  raise a clear `TypeError` — **never** silently fall back to a strong ref.
  First red test proves pydantic v2 `BaseModel` supports `weakref.ref`; the
  Ferro metaclass gains a class-definition-time guard so a user-added
  `__slots__` fails loudly there, not at runtime.

**Pruning — amortized sweep, no weakref callbacks.**

- get-side: any lookup hitting a dead ref removes that entry immediately;
- sweep: per-map atomic op counter triggers `retain(alive)` every ~1024 ops
  — O(map) per sweep, amortized O(1)/op.

Weakref callbacks (exact delete-on-death) are rejected: a callback fires
during refcount-drop/GC at unpredictable points and would re-enter the
`DashMap` — possibly on a shard lock already held by the same thread across
the FFI boundary (deadlock; AGENTS.md I-3). The sweep is lock-safe and
bounds memory at *live instances + ≤N tombstones* (tombstones are a key +
dead weakref, ~tens of bytes). Session close drops the whole map, as today.

## D1b — Refresh-on-load

On fetch-hit the decoded row is currently discarded
(`src/operations.rs:991–997`, ~`:1824`, single-get ~`:1054`). Instead:
write the fresh values into the cached live instance and return it —
`a is b` preserved, values current.

- **One materialization path.** Extract the field-materialization step of
  `hydrate_model_instance` (decoded values → Python objects, including FK
  columns and FF-C plan-driven enum hydration) into a helper shared by fresh
  hydration and refresh. Refresh: upgrade weakref → overwrite
  `instance.__dict__[field]` for **every** decoded field → reset
  `__pydantic_fields_set__` to the full decoded-field set (identical to a
  freshly hydrated instance) → preserve `_ferro_persisted`/origin internals.
  Shared path = refresh cannot drift from hydration; partial writes are
  structurally impossible.
- **Guarantee** (documented in `docs/pages/concepts/identity-map.md`):
  *within a session, fetching a row you already hold returns the same
  object, updated in place to the database's current values.* Corollary,
  stated explicitly: **unsaved local mutations are overwritten by a
  re-fetch — the database wins.** Merging is how stale-read bugs survive.

## D2 — Scoped invalidation + global-map deletion

- `rollback_transaction` (`src/operations.rs:825`) stops calling the global
  everything-clear; it evicts the affected `(connection, session)` scope:
  the session's map when `session_id` is set — and under Option B the
  sessionless arm has no map, so nothing to clear.
- Bulk update/delete evict per `(connection, model)` via the existing
  `identity_map_retain_model` shape (`src/operations.rs:148`), scoped to the
  session map.
- Delete `static IDENTITY_MAP` (`src/state.rs:212`) and its clear sites
  (`src/connection.rs:315`, `src/migrate.rs:886`). Not kept as a fallback;
  the identity primitives' `None` arm is a no-op, not a global.
- Session/transaction isolation semantics of `SessionState` are unchanged.

## D3 — One route handle

**Shape: frozen `#[pyclass]` passed by value — not a token into a registry**
(a registry adds allocation, an eviction policy, and a lifetime bug surface
for three small fields).

```rust
#[pyclass(frozen)]
pub struct RouteHandle {
    tx_id: Option<String>,      // Some → route through this tx's pinned conn
    connection_name: String,    // always resolved, never None
    session_id: Option<String>, // Some → session identity map; None → none
}
```

- **Resolved exactly once** in `resolve_operation_scope`
  (`src/ferro/state.py:65`; `resolve_transaction_scope` at `:122` likewise):
  Python reads the ambient contextvars (session, tx) and makes one FFI call
  that validates and returns the handle. Every operation signature collapses
  from `(tx_id=None, using=None, session_id=None)` (~22 signatures) to
  `(route)`. `active_route_for_operation` (`src/operations.rs:75`, 18
  callers) reduces to reading the handle's fields; engine lookup by
  `connection_name` stays a cheap map hit inside Rust (`Arc<EngineHandle>`
  does not cross the boundary).
- **`connection_name` is non-optional** — the ambient-default path is not an
  error branch to maintain; a routeless handle is unrepresentable.
- Rollout: change the Rust signatures first and let the compiler enumerate
  call sites; Python follows the new `resolve_operation_scope` return type
  through `models.py` / `raw.py` / `query/builder.py`. Grep-verified
  "one route-resolution site per operation" is the backstop.

## D4 — `using=` vs ambient session is an error

In the same single resolution site: explicit `using` ≠ ambient session's
connection → `ValueError` (replaces the silent `effective_session = None`
bypass at `src/ferro/state.py:86–87`). Explicit `using` ≠ explicit
`session=` already raises and continues to. No deprecation phase.

## D5 — `Model.__init__` FK extraction

`src/ferro/models.py:214–220` scans the **source** class's `ferro_fields`
for the PK name, then reads that name off the **target** instance — correct
today only because every PK is named `id`. Fix: resolve the PK field from
`val.__class__` (the target model). Test with a target whose PK ≠ `id`.

---

## Build order & commits

D5 (isolated warm-up) → D1 (weak map + refresh-on-load) → D4 + D3 (remove
ambient path, collapse routing) → D2 (scoped eviction, delete global map).
Separate conventional commits, scope `ff-d`, `!` on the user-observable
breaking commits (D4 error, ambient-path removal). Branch
`ff-d/identity-and-routing`.

## Testing (exit gates, written first, red before implementation)

1. **Memory bound:** loop loading ~1M rows in a session → bounded RSS after
   GC (strong-ref map fails today).
2. **Stale read:** external `UPDATE` + re-fetch → fresh values with
   `a is b` preserved (fails today).
3. **D5:** relationship input whose target model's PK ≠ `id` extracts the
   right value.
4. **Scoped invalidation:** rollback evicts only the affected
   `(connection, session)`; bulk update/delete only `(connection, model)`;
   an unrelated cached instance survives.
5. **One route site:** grep-verifiable single route-resolution per
   operation.
6. **`using` conflict:** explicit `using` ≠ ambient session raises
   `ValueError`; no-session no-`using` raises.
7. **Weakref support:** `weakref.ref(instance)` works on Ferro models;
   a weakref-suppressing class fails loudly at class definition.

Verification matrix: `cargo test` (crates + root
`--no-default-features --features testing`); full
`FERRO_POSTGRES_URL=... just test` green with the global map gone;
shadow-strict rerun where touched; grep gates (no `IDENTITY_MAP`, one
route-resolution site).
