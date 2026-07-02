"""The timing core: wall-clock measurement over a persistent event loop.

Deliberately not pytest / pytest-benchmark: that framework rebuilds the asyncio
event loop per iteration (loop setup pollutes an async measurement) and the
repo's global ``addopts = "--cov=src"`` would instrument the very hot path we
measure. Here a single loop is created once by the runner, warmup iterations are
discarded, and each timed unit is an already-built awaitable thunk so no
per-iteration construction cost leaks into the number.

Only wall-clock is measured (median/min/mean/stdev/p95). Absolute values are
machine- and backend-specific; the deliverable is stable *per-backend* deltas,
which ``compare.py`` computes.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

# A thunk producing a fresh awaitable for one timed iteration. Building the
# awaitable up front (outside the timed region) keeps construction cost out of
# the measurement.
Thunk = Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class Timing:
    """Wall-clock summary for one benchmark case, in milliseconds."""

    name: str
    backend: str
    rows: int
    n: int
    median_ms: float
    min_ms: float
    mean_ms: float
    stdev_ms: float
    p95_ms: float

    def as_record(self) -> dict[str, float | int]:
        """The per-benchmark payload written to the baseline JSON."""
        d = asdict(self)
        d.pop("name")
        d.pop("backend")
        return d


def _percentile(sorted_ms: list[float], pct: float) -> float:
    """Nearest-rank percentile over an ascending list (pct in [0, 100])."""
    if not sorted_ms:
        return 0.0
    if len(sorted_ms) == 1:
        return sorted_ms[0]
    rank = max(1, min(len(sorted_ms), round(pct / 100 * len(sorted_ms))))
    return sorted_ms[rank - 1]


def summarize(name: str, backend: str, rows: int, durations_s: list[float]) -> Timing:
    """Reduce raw per-iteration seconds into a millisecond ``Timing``."""
    ms = sorted(d * 1000.0 for d in durations_s)
    return Timing(
        name=name,
        backend=backend,
        rows=rows,
        n=len(ms),
        median_ms=statistics.median(ms),
        min_ms=ms[0],
        mean_ms=statistics.fmean(ms),
        stdev_ms=statistics.stdev(ms) if len(ms) > 1 else 0.0,
        p95_ms=_percentile(ms, 95),
    )


async def measure(
    name: str,
    backend: str,
    rows: int,
    thunks: list[Thunk],
    warmup: int,
) -> Timing:
    """Time each thunk after discarding ``warmup`` leading iterations.

    ``thunks`` holds ``warmup + n`` entries; the first ``warmup`` are awaited but
    not recorded (JIT-free Python still has cold caches, first-touch pool
    connections, and lazy imports to shake out). Each remaining thunk is timed
    individually with ``perf_counter`` around a single ``await``.
    """
    for i in range(warmup):
        await thunks[i]()

    durations: list[float] = []
    for thunk in thunks[warmup:]:
        start = time.perf_counter()
        await thunk()
        durations.append(time.perf_counter() - start)

    return summarize(name, backend, rows, durations)
