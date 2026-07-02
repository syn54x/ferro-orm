"""The rich-type fixture model and deterministic row generator.

The model is load-bearing: a plain ``int``/``str`` model would show nothing from
FF-C's C1–C4 rewrite. Every field here maps to a concrete cost those sub-tasks
remove, so the benchmark makes the codec/decode/catalog work visible:

- ``created_at: datetime`` via ``db_type="timestamptz"`` — C3 native typed decode
  (today coerced through a ``CAST(... AS text)`` projection on Postgres, F8).
- ``balance: Decimal`` — C1/F5 per-value codec (the heuristic ``pattern_looks_decimal``
  path C1 deletes).
- ``external_id: uuid.UUID`` via ``db_type="uuid"`` — C3 typed decode / UUID casing.
- ``status: Status`` (a ``str, Enum`` with no ``db_type``) — native Postgres enum,
  C4 enum hydration (removes the ``_fix_types`` Python post-pass).
- ``attributes: dict[str, str]`` — JSON codec.
- ``name: str`` / ``score: int`` — the plain-typed baseline columns.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from ferro import Field, Model


class Status(str, Enum):
    """A native-enum column (no ``db_type``) so C4's enum hydration is exercised."""

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class BenchRecord(Model):
    """Rich-type fixture spanning every codec/decode family C1–C4 rewrites."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    score: int
    created_at: datetime = Field(db_type="timestamptz")
    balance: Decimal
    external_id: uuid.UUID = Field(db_type="uuid")
    status: Status
    attributes: dict[str, str]


_STATUSES = tuple(Status)
# A fixed UTC instant so seed data never depends on wall-clock time (which is
# both nondeterministic and, for `new Date()`-style calls, unavailable in some
# harnesses). The generator derives per-row offsets from the seeded RNG only.
_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def make_records(n: int, seed: int) -> list[BenchRecord]:
    """Build ``n`` deterministic ``BenchRecord`` instances.

    Seeded so every run — and every backend — sees byte-identical input, which
    is what lets the timings be compared run-to-run. The rows are *unsaved*
    instances (no ``id``); callers persist them via ``create`` / ``bulk_create``.
    """
    rng = random.Random(seed)
    records: list[BenchRecord] = []
    for i in range(n):
        # UUID from the seeded RNG (uuid4() would pull from os.urandom and break
        # determinism); build a version-4-shaped value from 16 seeded bytes.
        uid = uuid.UUID(bytes=bytes(rng.getrandbits(8) for _ in range(16)), version=4)
        records.append(
            BenchRecord(
                name=f"record-{i:06d}",
                score=rng.randint(0, 1_000_000),
                created_at=_EPOCH.replace(microsecond=rng.randint(0, 999_999)),
                balance=Decimal(rng.randint(0, 10_000_000)) / Decimal(100),
                external_id=uid,
                status=_STATUSES[rng.randrange(len(_STATUSES))],
                attributes={
                    "region": rng.choice(("us", "eu", "apac")),
                    "tier": rng.choice(("free", "pro", "enterprise")),
                },
            )
        )
    return records
