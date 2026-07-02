"""Compare two benchmark result files and print per-benchmark deltas.

    uv run python -m benchmarks.compare OLD.json NEW.json

This is the "before/after C1–C3" tool: capture a baseline, make a hot-path
change, re-run, then diff. Deltas are reported on the median (Δms and Δ%);
negative % is a speed-up, positive is a regression. Compare only files from the
*same backend on the same machine* — absolute numbers are not portable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "benchmarks" not in data:
        raise ValueError(f"{path}: not a benchmark result file (no 'benchmarks' key)")
    return data


def _rows(
    old: dict, new: dict
) -> list[tuple[str, float | None, float | None, float | None]]:
    old_b, new_b = old["benchmarks"], new["benchmarks"]
    out = []
    for name in sorted(set(old_b) | set(new_b)):
        o = old_b.get(name, {}).get("median_ms")
        n = new_b.get(name, {}).get("median_ms")
        pct = ((n - o) / o * 100.0) if (o and n is not None) else None
        out.append((name, o, n, pct))
    return out


def _print(old: dict, new: dict, old_path: Path, new_path: Path) -> None:
    ob, nb = old.get("meta", {}).get("backend"), new.get("meta", {}).get("backend")
    if ob and nb and ob != nb:
        print(f"⚠️  backend mismatch: old={ob!r} new={nb!r} — deltas are meaningless\n")

    rows = _rows(old, new)
    try:
        from rich.console import Console
        from rich.table import Table

        title = f"{old_path.name} → {new_path.name}"
        if ob:
            title += f"  ({ob})"
        table = Table(title=f"median Δ  {title}")
        table.add_column("benchmark", justify="left")
        table.add_column("old (ms)", justify="right")
        table.add_column("new (ms)", justify="right")
        table.add_column("Δ ms", justify="right")
        table.add_column("Δ %", justify="right")
        for name, o, n, pct in rows:
            d_ms = f"{n - o:+.3f}" if (o is not None and n is not None) else "—"
            if pct is None:
                d_pct = "—"
                style = "dim"
            else:
                d_pct = f"{pct:+.1f}%"
                style = "green" if pct < 0 else ("red" if pct > 0 else None)
            table.add_row(
                name,
                f"{o:.3f}" if o is not None else "—",
                f"{n:.3f}" if n is not None else "—",
                d_ms,
                f"[{style}]{d_pct}[/{style}]" if style else d_pct,
            )
        Console().print(table)
    except ImportError:  # pragma: no cover - rich is a dev dependency
        print(f"{'benchmark':<26}{'old':>10}{'new':>10}{'Δ%':>9}")
        for name, o, n, pct in rows:
            print(
                f"{name:<26}"
                f"{(f'{o:.3f}' if o is not None else '—'):>10}"
                f"{(f'{n:.3f}' if n is not None else '—'):>10}"
                f"{(f'{pct:+.1f}%' if pct is not None else '—'):>9}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff two benchmark result files (median deltas)."
    )
    parser.add_argument("old", type=Path, help="baseline result JSON")
    parser.add_argument("new", type=Path, help="new result JSON")
    args = parser.parse_args(argv)

    _print(_load(args.old), _load(args.new), args.old, args.new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
