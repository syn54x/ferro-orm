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
and `migrate()`. Alembic autogenerate calls `ensure_resolved_modelset()` only.

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

- Rust registration is empty until the first successful sync call.
- `register_model_with_ir` must not call `register_model_schema` directly.
- Bulk push must be atomic (all models + modelset, or none).
- Rust stores the modelset fingerprint after install for the clean-path skip.
- `compile_registry_schema_ir()` becomes an assemble step over
  `_SCHEMA_IR_BY_MODEL`, not a full recompile pass.
