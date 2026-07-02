"""Ferro hot-path benchmark suite (Epic FF-C, sub-task C5).

A small, pinned, repeatable suite measuring the Python-facing async hot path —
single save, bulk save, and a filtered fetch of ~10k rows — on both backends
(SQLite always; Postgres when ``FERRO_POSTGRES_URL`` is configured), over a
rich-type fixture model that exercises the type-decision path FF-C's C1–C4
rewrite touches.

The suite is purely additive: it imports the public Ferro API and never changes
production code. Run it with ``just bench`` (see ``benchmarks/README.md``).
"""
