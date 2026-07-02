"""FF-C C1: per-model codec plan behavior pinned at the Python boundary.

The F5 regression (roadmap `docs/plans/2026-07-02-001-fable-fixes-roadmap.md`,
Epic FF-C): the legacy pattern sniffer classified any ``str`` field whose
``pattern`` looked numeric as a decimal column. Observable damage today:

- SQLite: the INSERT bind round-trips the string through ``f64``, so
  ``"2024"`` is stored as ``'2024.0'`` and ``"007207"`` as ``'7207.0'`` —
  silent on-disk corruption.
- Postgres: WHERE binds emit ``CAST($1 AS numeric)`` against the varchar
  column — ``operator does not exist: character varying = numeric``.

Column types are decided once per model from the FF-B storage decision table;
a ``pattern=`` constraint must never change a column's type.
"""

from __future__ import annotations

import pytest

from ferro import Field, Model, connect
from ferro.raw import fetch_all


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_str_field_with_numeric_pattern_round_trips_as_str(db_url):
    """F5 repro: a numeric-looking pattern on a str field must stay a str."""

    class Vehicle(Model):
        id: int | None = Field(default=None, primary_key=True)
        year_code: str = Field(pattern=r"^\d{4}$")

    await connect(db_url, auto_migrate=True)

    row = await Vehicle.create(year_code="2024")
    assert type(row.year_code) is str

    fetched = await Vehicle.get(row.id)
    assert fetched is not None
    assert type(fetched.year_code) is str
    assert fetched.year_code == "2024"

    # The stored value must be the exact string — not an f64 round-trip
    # ('2024.0') and not a numeric-cast rewrite.
    raw = await fetch_all("SELECT year_code FROM vehicle")
    assert raw == [{"year_code": "2024"}]

    filtered = await Vehicle.where(lambda v: v.year_code == "2024").all()
    assert len(filtered) == 1
    assert type(filtered[0].year_code) is str


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_nullable_str_field_with_leading_zero_pattern_value(db_url):
    """F5 anyOf arm: Optional[str] with a numeric pattern keeps leading zeros."""

    class Part(Model):
        id: int | None = Field(default=None, primary_key=True)
        serial: str | None = Field(default=None, pattern=r"^\d{6}$")

    await connect(db_url, auto_migrate=True)

    row = await Part.create(serial="007207")
    fetched = await Part.get(row.id)
    assert fetched is not None
    assert type(fetched.serial) is str
    assert fetched.serial == "007207"

    # `get` can serve the identity-mapped instance; the stored value is the
    # honest witness. Leading zeros must survive (no f64 round-trip).
    raw = await fetch_all("SELECT serial FROM part WHERE serial IS NOT NULL")
    assert raw == [{"serial": "007207"}]

    empty = await Part.create()
    refetched = await Part.get(empty.id)
    assert refetched is not None
    assert refetched.serial is None
