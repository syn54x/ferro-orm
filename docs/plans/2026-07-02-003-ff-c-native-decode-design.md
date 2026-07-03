# FF-C C3+C4 — Native typed decode on Postgres; enum hydration in Rust

Roadmap: `docs/plans/2026-07-02-001-fable-fixes-roadmap.md` (C3 L195–202, C4
L203–207). Input: the merged C1 per-model `ColumnCodec` plan (`codec_plan.rs`).

## Problem

Postgres reads are coerced to "SQLite-shaped text" by wrapping rich-typed
columns in `CAST(... AS text)` in the SELECT projection
(`apply_postgres_text_select_columns`), then re-parsed on the Python side.
This couples `timestamptz` reads to the session `TimeZone`, adds per-query
projection machinery plus catalog lookups, and leaves `EngineValue` with six
coarse variants. Separately, enum coercion is a per-instance Python post-pass
(`Model._fix_types`, `except Exception: pass`) run after every fetch with
partial coverage (C4 / I-6).

## Decisions

### D1 — How the plan reaches the decode path

Decode has two layers with different authorities:

1. **Wire layer (`backend.rs`)** — decodes what is physically on the wire.
   Postgres gets a backend-specific row materializer keyed on sqlx's column
   *type info* (the wire is physical: a `numeric` column must be decoded as
   numeric no matter what the plan says, and fetches also return off-plan
   joined/m2m/raw columns). SQLite keeps the existing 6-variant ladder — its
   wire really is text/int/real/blob.
2. **Shape layer (`codec.rs::decode_engine_value`)** — already receives the
   C1 plan per column; stays the single authority for the *Python-facing*
   value shape. It maps typed `EngineValue` + `ColumnCodec` → `RustValue`,
   and on SQLite parses stored text into the *same* typed values. This is
   where "UUID stored as text still hydrates `uuid.UUID`" lives, exactly as
   the C1 plan intends (logical codec wins over widened storage).

No plan threading into `backend.rs`; no re-derivation of column types.

### D2 — Typed variants and Python mapping

- `Cargo.toml`: sqlx features += `chrono`, `rust_decimal`, `json`;
  pyo3 features += `chrono` (abi3-compatible datetime construction).
- `EngineValue` += `Uuid(uuid::Uuid)`, `TimestampTz(DateTime<Utc>)`,
  `Timestamp(NaiveDateTime)`, `Date(NaiveDate)`, `Time(NaiveTime)`,
  `Decimal(String)` (decoded via `rust_decimal`, transported as string to
  preserve scale/trailing zeros), `Json(serde_json::Value)`.
- `RustValue` += typed temporal variants constructed directly via pyo3-chrono
  (no ISO-string round trip); existing `Uuid(String)`/`Decimal(String)`
  transports stay (lowercase hyphenated UUID; `Decimal(str)` preserves
  precision). Legacy string variants remain as parse fallback so unparseable
  legacy text keeps today's behavior.
- Python mapping: `timestamptz` → tz-aware `datetime` in UTC **independent of
  session `TimeZone`** (binary wire is epoch-based); `timestamp` → naive
  `datetime`; `date` → `datetime.date`; `time` → `datetime.time` (**breaking**:
  was `str`); `uuid` → `uuid.UUID`; `numeric` → `decimal.Decimal`;
  `json/jsonb` → parsed object; enum UDT → label text at the wire layer.

### D3 — Enum hydration (C4)

The "registry that holds the enum class" is the model class itself:
`cls._enum_fields` (populated by the metaclass). Fetch paths build a
per-fetch map of interned column name → enum class once, and
`hydrate_model_instance` converts non-null enum-column values via
`enum_cls(value)` — one plan-consistent conversion at the hydration boundary,
inside Rust, no Python post-pass. Conversion failures raise a `ValueError`
with model/column context instead of silently passing. `_fix_types` and all
six call sites are deleted. No Python objects enter the static Rust registry
(avoids GC/lifetime hazards).

## Deletions

- `apply_postgres_text_select_columns` (codec.rs + operations.rs wrapper +
  call sites at operations.rs:985/1135/1790) — projection becomes plain
  column list / `*`.
- `ModelCodecPlan::pg_text_projection`, `codec_needs_pg_text_projection`.
- SELECT-path `postgres_enum_udt_by_column` lookups that only fed the
  projection (bind-path lookups stay; C2 owns caching).
- `Model._fix_types` + 6 call sites.

## Test strategy (non-negotiable ordering)

1. **First**, `tests/test_hydration_equivalence.py` (`backend_matrix`, rich
   full-type model): asserts hydrated Python values are *exactly equal*
   across SQLite and Postgres — aware + naive `datetime`, `date`, `time`,
   `UUID` (exact equality, not `str()`), `Decimal` incl. trailing-zero scale,
   JSON, `bytes`, str/int enums, bool, float, None. Green on the current
   text-decode path before any Rust change; stays green across the swap.
2. Deliberately-red TDD tests alongside (xfail-strict pre-swap): session
   `TimeZone` set non-UTC leaves the hydrated `timestamptz` value and its
   `utcoffset()` unchanged; `time` hydrates `datetime.time`; enum members on
   every fetch path; SELECT SQL contains no `CAST(... AS text)`.
3. Shadow-strict rerun where touched (`FERRO_SHADOW_RUNTIME=1` +
   `FERRO_SHADOW_RUNTIME_STRICT=1`).
4. CodecIR `wire_kinds` tokens stay unchanged (settled during
   implementation): the vocabulary is shared by bind and fetch rules, and the
   bind wire is still text + `CAST` on Postgres — the `*_text` tokens keep
   describing the bind side and the SQLite fetch side truthfully. The fetch
   side's `python_kind` contract was already the native shape
   (`datetime.time`, `enum.Enum`) and is now actually delivered. Splitting
   bind/fetch wire vocabularies becomes relevant only if the bind path goes
   native (future work).

## Out of scope

C2 (epoch catalog cache), any re-derivation of column types outside the C1
plan, identity-map/routing work (FF-D).
