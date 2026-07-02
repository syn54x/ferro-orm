"""Run the benchmark suite and write per-backend baseline JSON.

    uv run python -m benchmarks.run                 # all available backends
    uv run python -m benchmarks.run --backends sqlite
    uv run python -m benchmarks.run --rows 10000 --out /tmp/before

Runs four cases per backend over the rich-type ``BenchRecord`` fixture:
``single_save``, ``bulk_save`` (``--rows``), and the filtered fetch of ~``--rows``
rows with the identity map both on (``fetch_10k_identity_on``) and off
(``fetch_10k_identity_off``). Postgres is included only when a URL is configured
(``FERRO_POSTGRES_URL`` / ``.env``); otherwise it is skipped with a printed note.

All cases share one persistent asyncio event loop. Each case runs against a fresh
isolated database (temp SQLite file / dropped-and-recreated PG schema), so a run
never depends on leftover state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from ferro import PoolConfig, connect, engines, reset_engine

from benchmarks.backends import available_backends, fresh_target
from benchmarks.harness import Timing, measure
from benchmarks.model import BenchRecord, make_records

# Pinned so pool warmth and connection count are not a hidden variable.
_POOL = PoolConfig(max_connections=5, min_connections=0)
_DEFAULT_SEED = 1234

# Rows per bulk INSERT statement. A single statement of `rows` × 8 columns blows
# past SQLite's bound-parameter cap (and Postgres' 65535), so a "bulk save N"
# realistically chunks. 1000 rows × 8 cols = 8000 binds — safely under both
# backends' limits — and the timed unit inserts all `rows` across these chunks.
_BULK_CHUNK = 1000


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def _insert_chunked(records) -> None:
    for chunk in _chunks(records, _BULK_CHUNK):
        await BenchRecord.bulk_create(chunk)


# Per-case iteration counts. Single save and fetch are cheap enough for a large
# sample; bulk save inserts --rows per iteration, so it uses a smaller sample to
# bound both wall-clock and the prebuilt-instance memory footprint. A global
# --iters/--warmup overrides these when supplied.
_CASE_ITERS = {
    "single_save": (100, 10),
    "bulk_save": (8, 2),
    "fetch": (30, 5),
}


def _counts(case: str, iters: int | None, warmup: int | None) -> tuple[int, int]:
    default_iters, default_warmup = _CASE_ITERS[case]
    return (
        iters if iters is not None else default_iters,
        warmup if warmup is not None else default_warmup,
    )


async def _bench_single_save(backend, url, rows, seed, iters, warmup) -> Timing:
    n, w = _counts("single_save", iters, warmup)
    await connect(url, auto_migrate=True, pool=_POOL)
    # An unnamed session binds the default connection so operations use the
    # supported routing path (not the v0.14-deprecated implicit-default path).
    async with engines.session():
        # One distinct unsaved instance per iteration; ``save()`` inserts a new
        # row (autoincrement PK), so iterations never collide or become UPDATEs.
        records = make_records(n + w, seed)
        thunks = [(lambda r=r: r.save()) for r in records]
        return await measure("single_save", backend, 1, thunks, w)


async def _bench_bulk_save(backend, url, rows, seed, iters, warmup) -> Timing:
    n, w = _counts("bulk_save", iters, warmup)
    await connect(url, auto_migrate=True, pool=_POOL)
    async with engines.session():
        # Distinct batch per iteration (different seed) so re-inserting never
        # hits a PK conflict and each iteration writes genuinely fresh rows. Each
        # timed unit inserts all `rows` via chunked bulk_create (see _BULK_CHUNK).
        batches = [make_records(rows, seed + i + 1) for i in range(n + w)]
        thunks = [(lambda b=b: _insert_chunked(b)) for b in batches]
        return await measure("bulk_save", backend, rows, thunks, w)


async def _bench_fetch(
    backend, url, rows, seed, iters, warmup, *, identity_map
) -> Timing:
    n, w = _counts("fetch", iters, warmup)
    await connect(url, auto_migrate=True, pool=_POOL, identity_map=identity_map)
    async with engines.session():
        await _insert_chunked(make_records(rows, seed))  # untimed seed
        # A filter matching every seeded row: a real WHERE clause returning ~rows
        # rows to hydrate each iteration. Repeated identical query exercises the
        # identity-map reconcile path (on) vs. fresh-instance hydration (off).
        name = f"fetch_{rows}_identity_{'on' if identity_map else 'off'}"
        thunks = [(lambda: BenchRecord.where(lambda r: r.score >= 0).all())] * (n + w)
        return await measure(name, backend, rows, thunks, w)


# (case-key, coroutine factory) in report order.
_CASES = [
    ("single_save", lambda **kw: _bench_single_save(**kw)),
    ("bulk_save", lambda **kw: _bench_bulk_save(**kw)),
    ("fetch", lambda **kw: _bench_fetch(**kw, identity_map=True)),
    ("fetch", lambda **kw: _bench_fetch(**kw, identity_map=False)),
]


def _run_backend(loop, backend, rows, seed, iters, warmup) -> list[Timing]:
    timings: list[Timing] = []
    for _case_key, factory in _CASES:
        with fresh_target(backend) as url:
            try:
                timings.append(
                    loop.run_until_complete(
                        factory(
                            backend=backend,
                            url=url,
                            rows=rows,
                            seed=seed,
                            iters=iters,
                            warmup=warmup,
                        )
                    )
                )
            finally:
                reset_engine()
    return timings


def _write_baseline(
    out_dir: Path, backend: str, timings: list[Timing], meta: dict
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {**meta, "backend": backend},
        "benchmarks": {t.name: t.as_record() for t in timings},
    }
    path = out_dir / f"{backend}.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _print_table(all_timings: list[Timing]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="Ferro hot-path benchmarks (wall-clock, ms)")
        for col in (
            "backend",
            "benchmark",
            "rows",
            "n",
            "median",
            "min",
            "mean",
            "stdev",
            "p95",
        ):
            table.add_column(
                col, justify="right" if col not in ("backend", "benchmark") else "left"
            )
        for t in all_timings:
            table.add_row(
                t.backend,
                t.name,
                str(t.rows),
                str(t.n),
                f"{t.median_ms:.3f}",
                f"{t.min_ms:.3f}",
                f"{t.mean_ms:.3f}",
                f"{t.stdev_ms:.3f}",
                f"{t.p95_ms:.3f}",
            )
        Console().print(table)
    except ImportError:  # pragma: no cover - rich is a dev dependency
        header = f"{'backend':<9}{'benchmark':<26}{'rows':>7}{'n':>5}{'median':>10}{'p95':>10}"
        print(header)
        for t in all_timings:
            print(
                f"{t.backend:<9}{t.name:<26}{t.rows:>7}{t.n:>5}"
                f"{t.median_ms:>10.3f}{t.p95_ms:>10.3f}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Ferro hot-path benchmark suite."
    )
    parser.add_argument(
        "--rows", type=int, default=10_000, help="rows for bulk save / fetch"
    )
    parser.add_argument(
        "--iters", type=int, default=None, help="override per-case timed iterations"
    )
    parser.add_argument(
        "--warmup", type=int, default=None, help="override per-case warmup iterations"
    )
    parser.add_argument(
        "--seed", type=int, default=_DEFAULT_SEED, help="deterministic data seed"
    )
    parser.add_argument(
        "--backends", type=str, default=None, help="comma list; default all available"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "baselines",
        help="directory for <backend>.json baselines",
    )
    args = parser.parse_args(argv)

    backends = available_backends()
    if args.backends:
        requested = [b.strip() for b in args.backends.split(",") if b.strip()]
        skipped = [b for b in requested if b not in backends]
        for b in skipped:
            print(f"note: backend {b!r} not available (no URL configured?) — skipping")
        backends = [b for b in requested if b in backends]
    if "postgres" not in backends:
        print(
            "note: Postgres not configured (set FERRO_POSTGRES_URL) — running SQLite only"
        )

    meta = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "rows": args.rows,
        "seed": args.seed,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Wall-clock ms. Machine-specific; compare same-machine runs only.",
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    all_timings: list[Timing] = []
    try:
        for backend in backends:
            timings = _run_backend(
                loop, backend, args.rows, args.seed, args.iters, args.warmup
            )
            path = _write_baseline(args.out, backend, timings, meta)
            print(f"wrote {path}")
            all_timings.extend(timings)
    finally:
        loop.close()

    _print_table(all_timings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
