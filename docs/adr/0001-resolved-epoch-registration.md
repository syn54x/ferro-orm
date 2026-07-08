# Resolved-epoch registration with deferred Rust sync

Ferro compiles each model to SchemaIR and uses that artifact for runtime codecs,
DDL emission, and auto-migrate. Registration was split across four stores updated
on different schedules (Python per-model IR, Python modelset, Rust column
registry, Rust modelset), with full recompilation on every `connect()`.

We adopt a two-phase registration model. **Provisional registration** (class
body execution) writes Python caches only and bumps a generation counter.
**Resolved registration** (after `resolve_relationships()`) assembles the
modelset from per-model envelopes without recompiling. Rust state is installed
only via `push_registration_to_rust()` — a single bulk FFI that atomically sets
`MODEL_REGISTRY` and `SCHEMA_IR_MODELSET`. A fingerprint gate skips the push
when Rust already holds the current modelset.

`ensure_resolved_modelset()` and `push_registration_to_rust()` compose
`_ensure_rust_registration_synced()`, shared by `connect()`, `create_tables()`,
`migrate()`, and the ORM operation seam — save/get/filter/delete run the O(1)
dirty check before touching the runtime, so a model defined after `connect()`
is synced on first use. Alembic autogenerate calls `ensure_resolved_modelset()`
only.

## Considered options

- **Incremental modelset push at import** — rejected because M2M join tables and
  shadow FK columns do not exist until relationship resolution completes.
- **Full resolve on every connect** — rejected as redundant when the generation
  counter is clean; reconnect would recompile N models for no change.
- **Per-model FFI replay on clean connect** — rejected in favor of one bulk
  bundle; partial replay leaves inconsistent Rust state on mid-loop failure.
- **Immediate Rust `MODEL_REGISTRY` push at import** — rejected to keep a single
  Rust sync seam; query building is Python-only until execution.

## Consequences

- Rust registration is empty until the first successful sync call; the
  operation seam makes that window invisible — absence is never a steady state.
- The sync primitive is registration-only and emits zero DDL: table creation
  and migration remain exclusive to `create_tables()`, `migrate()`, and
  `connect(auto_migrate=True)` — a save or query can never alter database
  schema.
- The dirty-path resolve + bulk install is single-flight (a thread-safe lock
  around confirm-dirty → resolve → install), so concurrent callers cannot
  double-compile or install stale-then-fresh. The clean-path check is one
  in-process generation-counter comparison with no FFI; the stored fingerprint
  is read only inside the push path.
- A failed resolve leaves the registry dirty and pending relations intact, so
  the next sync retries from a consistent state — the Python mirror of the
  install's retained-last-good guarantee.
- `register_model_with_ir` must not call `register_model_schema` directly.
- Bulk push must be atomic (all models + modelset + fingerprint, or none):
  build-then-swap — the payload is constructed and validated before the lock
  is taken; a failed install retains the last good registration, never an
  empty runtime.
- Rust stores the modelset fingerprint after install for the clean-path skip.
  The fingerprint is part of the atomic install unit: set only on successful
  install, cleared by `clear_registry()` in the same lock scope.
- `compile_registry_schema_ir()` becomes an assemble step over
  `_SCHEMA_IR_BY_MODEL`, not a full recompile pass. It iterates
  `_MODEL_REGISTRY_PY` plus `_JOIN_TABLE_REGISTRY` (never the envelope cache
  directly) and fails loudly on a missing envelope. A dirty resolve recompiles
  only the models whose canonical schema changed during resolution, including
  shadow-FK type reconciliation targets — "no recompilation" is the clean
  path's property.
- Model removal goes through a deregistration entrypoint that bumps the
  generation counter and evicts the model's envelope; fixtures stop mutating
  `_MODEL_REGISTRY_PY` directly.
- `clear_registry()` also evicts join-table envelopes alongside its
  `_JOIN_TABLE_REGISTRY` purge, preserving the #153 stale-join-table guard.
