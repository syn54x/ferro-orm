# Ferro hot-path benchmarks

A small, pinned, repeatable suite that measures Ferro's Python-facing async hot
path — **single save**, **bulk save**, and a **filtered fetch of ~10k rows** — on
both backends, over a rich-type fixture model. It exists so the before/after of
Epic **FF-C** (per-model codec plans, catalog cache, native Postgres decode, enum
hydration) is quantifiable and future hot-path regressions are visible.

It is **purely additive**: it imports the public Ferro API and changes no
production code. It is **not** part of `just test`, so it never slows the
functional matrix.

## What it measures

Four cases per backend, over `BenchRecord` (see `model.py`):

| benchmark                 | operation                                             | rows |
| ------------------------- | ----------------------------------------------------- | ---- |
| `single_save`             | `record.save()` — one INSERT                          | 1    |
| `bulk_save`               | `BenchRecord.bulk_create([...])` — one bulk INSERT    | 10k  |
| `fetch_10k_identity_on`   | `BenchRecord.where(...).all()`, identity map **on**   | 10k  |
| `fetch_10k_identity_off`  | `BenchRecord.where(...).all()`, identity map **off**  | 10k  |

Only **wall-clock** is recorded (median / min / mean / stdev / p95, in ms).

### Why the rich-type fixture is load-bearing

A plain `int`/`str` model would show *nothing* from FF-C. Every `BenchRecord`
field maps to a concrete cost C1–C4 removes, so the suite makes that work
visible:

- `created_at: datetime` (`db_type="timestamptz"`) → **C3** native typed decode
  (today a `CAST(... AS text)` projection on Postgres — finding **F8**).
- `balance: Decimal` → **C1/F5** per-value codec (the `pattern_looks_decimal`
  heuristic C1 deletes).
- `external_id: uuid.UUID` (`db_type="uuid"`) → **C3** typed decode / UUID casing.
- `status: Status` (`str, Enum`, no `db_type`) → native Postgres enum, **C4**
  enum hydration (removes the `_fix_types` Python post-pass).
- `attributes: dict[str, str]` → JSON codec.
- `name`, `score` → the plain-typed baseline columns.

Catalog round-trips (**F4**) are incurred by all of the above during
save/fetch/migrate, so they are exercised implicitly.

## Running

```bash
just bench                 # SQLite always; Postgres if FERRO_POSTGRES_URL is set
# or directly:
uv run python -m benchmarks.run
```

Postgres is included automatically when a URL is configured (via
`FERRO_POSTGRES_URL` or a repo-root `.env`, reusing `tests/db_backends.py`); it is
skipped with a printed note otherwise.

Useful flags: `--rows N`, `--iters N`, `--warmup N`, `--seed N`,
`--backends sqlite,postgres`, `--out DIR`.

Each run writes `baselines/<backend>.json` (per-backend, keyed by benchmark name)
and prints a summary table.

## Capturing a before/after around a change

```bash
# 1. Baseline the current code into a scratch dir
uv run python -m benchmarks.run --out /tmp/before

# 2. Make your hot-path change, rebuild the Rust core
uv run maturin develop

# 3. Re-run into a second dir
uv run python -m benchmarks.run --out /tmp/after

# 4. Diff the medians (per backend)
uv run python -m benchmarks.compare /tmp/before/sqlite.json /tmp/after/sqlite.json
uv run python -m benchmarks.compare /tmp/before/postgres.json /tmp/after/postgres.json
```

`compare` prints each benchmark's `old → new` median with **Δms** and **Δ%**.
Negative % is a speed-up (green); positive is a regression (red).

## Reading the deltas

- Compare **same backend, same machine** only. Absolute numbers vary wildly by
  backend and hardware; the deliverable is a stable *per-backend delta*, never a
  SQLite-vs-Postgres comparison.
- The checked-in `baselines/*.json` were captured on a developer machine and are
  a reference point, not a CI gate. Re-capture your own baseline before measuring
  a change so both sides come from the same box.
- Watch the **median** for the headline and **stdev/p95** for noise. If stdev is
  a large fraction of the median, raise `--warmup`/`--iters` until it settles
  before trusting a delta.

## Stability knobs

Fixed row counts, deterministic seed data (`random.Random(seed)`), discarded
warmup iterations, a fresh database per case (temp SQLite file / dropped-and-
recreated PG schema), a pinned pool (`max_connections=5`), and a single
persistent event loop across all cases. The fetch cases are measured with the
identity map both on and off because that bookkeeping is real hot-path cost the
FF-C rewrite interacts with.
