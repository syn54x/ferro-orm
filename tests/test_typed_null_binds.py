"""Integration tests for typed-null binds (#38).

The original bug was a PostgreSQL-only "null is text" failure. Postgres rejects
``null::text`` for ``integer``/``bigint``/``bool``/``numeric``/``bytea``/
``uuid`` columns because there's no implicit cast. SQLite is type-permissive
and never reproduced #38; many of the round-trip assertions here are
``postgres_only`` because that's where the failure shape lives.

Two known pre-existing bugs are *out of scope* for this refactor and limit
what the matrix can assert. When either is fixed, drop the corresponding
``xfail``/``postgres_only`` marker on the test below.

1. `#41 <https://github.com/syn54x/ferro-orm/issues/41>`_ --
   ``Model.where(Model.col == None)`` panics in the Rust query builder
   (``query.rs::node_to_condition_for_backend`` unwraps ``node.value``).
   Backend-agnostic: surfaces on both SQLite and Postgres before any SQL
   is generated, so ``test_filter_by_none_does_not_reproduce_38`` is
   ``xfail(strict=True)`` until #41 closes -- the strict marker means the
   test will XPASS-as-failure the moment #41 is fixed, prompting us to
   drop the marker.
2. `#42 <https://github.com/syn54x/ferro-orm/issues/42>`_ --
   ``UPDATE col = NULL`` on SQLite reads back as ``0`` (or the type's zero
   value) due to a hydration issue in ``materialize_engine_row``. SQLite-
   specific, so ``test_update_to_none_for_each_type`` is ``postgres_only``.

See ``docs/plans/2026-04-29-001-typed-null-binds-plan.md`` for context.
"""

import uuid
from decimal import Decimal
from typing import Annotated

import pytest

from ferro import FerroField, Model, connect


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_insert_with_none_for_each_nullable_primitive(db_url):
    """Each in-scope nullable primitive accepts ``None`` on INSERT and
    round-trips correctly. Direct regression for #38 on Postgres; parity
    check on SQLite."""

    class Mixed(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        count: int | None = None
        active: bool | None = None
        ratio: float | None = None
        name: str | None = None
        blob: bytes | None = None

    await connect(db_url, auto_migrate=True)

    row = await Mixed.create()
    assert row.id is not None
    assert row.count is None
    assert row.active is None
    assert row.ratio is None
    assert row.name is None
    assert row.blob is None

    fetched = await Mixed.get(row.id)
    assert fetched is not None
    assert fetched.count is None
    assert fetched.active is None
    assert fetched.ratio is None
    assert fetched.name is None


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_insert_with_value_round_trips_for_each_type(db_url):
    """Non-null values for each nullable type round-trip correctly."""

    class Mixed(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        count: int | None = None
        active: bool | None = None
        ratio: float | None = None
        name: str | None = None

    await connect(db_url, auto_migrate=True)

    row = await Mixed.create(count=42, active=True, ratio=3.14, name="ferro")
    fetched = await Mixed.get(row.id)
    assert fetched is not None
    assert fetched.count == 42
    assert fetched.active is True
    assert fetched.ratio == pytest.approx(3.14)
    assert fetched.name == "ferro"


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_insert_with_none_for_uuid(db_url):
    """Nullable UUID columns accept ``None`` on INSERT and round-trip.

    Pre-refactor on Postgres this required ``cast_as("uuid")`` workarounds in
    the Rust core; the typed-bind layer now sends a properly-typed UUID OID
    without SQL-text manipulation."""

    class WithUuid(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        run_id: uuid.UUID | None = None

    await connect(db_url, auto_migrate=True)

    null_row = await WithUuid.create()
    fetched_null = await WithUuid.get(null_row.id)
    assert fetched_null is not None
    assert fetched_null.run_id is None

    u = uuid.uuid4()
    set_row = await WithUuid.create(run_id=u)
    fetched_set = await WithUuid.get(set_row.id)
    assert fetched_set is not None
    assert fetched_set.run_id == u


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_insert_with_none_for_decimal(db_url):
    """Nullable Decimal columns accept ``None`` on INSERT.

    Decimal is currently bound as ``float8``-typed null on Postgres; native
    ``numeric`` typed binds are deferred (plan §3 Scope Boundaries). This
    test asserts user-facing behavior, not the wire-level type.
    """

    class WithDecimal(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount: Decimal | None = None

    await connect(db_url, auto_migrate=True)

    null_row = await WithDecimal.create()
    fetched = await WithDecimal.get(null_row.id)
    assert fetched is not None
    assert fetched.amount is None


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_issue_38_exact_regression(db_url):
    """Literal reproduction of #38: ``bench_level: int | None = None`` on
    Postgres INSERT used to fail with::

        column "bench_level" is of type integer but expression is of type text

    The typed-null-binds refactor fixes this. Postgres-only because the
    failure mode is OID-specific.
    """

    class Scorecard(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        bench_level: int | None = None

    await connect(db_url, auto_migrate=True)

    row = await Scorecard.create()
    assert row.id is not None
    assert row.bench_level is None


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_update_to_none_succeeds_on_postgres(db_url):
    """Setting a previously-set column back to ``None`` via UPDATE succeeds
    on Postgres after the typed-null-binds refactor.

    Pre-refactor the Postgres OID error fired on this path too; the fix is
    in U6 (`value_rhs_simple_expr_for_backend`) plus the bind layer (U3).

    SQLite has a separate, pre-existing hydration bug for ``UPDATE col =
    NULL`` (see #42) that's out of scope for this refactor, so this
    assertion is Postgres-only.
    """

    class Mixed(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        count: int | None = None
        active: bool | None = None
        name: str | None = None

    await connect(db_url, auto_migrate=True)

    row = await Mixed.create(count=99, active=True, name="initial")
    updated = await Mixed.where(Mixed.id == row.id).update(
        count=None, active=None, name=None
    )
    assert updated == 1

    fetched = await Mixed.get(row.id)
    assert fetched is not None
    assert fetched.count is None
    assert fetched.active is None
    assert fetched.name is None


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_update_to_none_executes_without_error(db_url):
    """Backend-matrix regression for #38 on the UPDATE path: the statement
    must execute without a Postgres OID type error or any other engine-side
    rejection.

    This is the strongest assertion that's portable across both backends,
    given the pre-existing SQLite hydration bug for ``UPDATE col = NULL``.
    """

    class Mixed(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        count: int | None = None

    await connect(db_url, auto_migrate=True)

    row = await Mixed.create(count=99)
    updated = await Mixed.where(Mixed.id == row.id).update(count=None)
    assert updated == 1


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Blocked by #41: filter `col == None` panics in "
        "node_to_condition_for_backend (Option::unwrap on node.value) "
        "before any SQL reaches the backend. Strict so we get an XPASS "
        "signal the moment #41 is fixed, then drop this marker."
    ),
)
async def test_filter_by_none_does_not_reproduce_38(db_url):
    """Query filter ``WHERE col == None`` on a nullable integer column must
    not fail with a Postgres OID type error.

    Backend-agnostic: the panic from #41 short-circuits this test on every
    backend, not just SQLite. Confirmed reproduced on Postgres while running
    the full matrix during the typed-null-binds refactor."""

    class Filterable(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        count: int | None = None

    await connect(db_url, auto_migrate=True)

    await Filterable.create(count=1)
    await Filterable.create()  # count = None

    # The assertion is "no Postgres OID error", not match counts.
    matched = await Filterable.where(Filterable.count == None).all()  # noqa: E711
    assert isinstance(matched, list)


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_invalid_uuid_raises_pyvalueerror_or_pydantic_error(db_url):
    """Invalid UUID strings on UUID columns surface a clean error.

    The Rust core's `schema_value_expr` raises ``PyValueError`` with a
    diagnostic naming the model, column, and offending value. Pydantic's
    own validation may catch the value first (equally good UX) -- we accept
    either.
    """

    class WithUuid(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        run_id: uuid.UUID | None = None

    await connect(db_url, auto_migrate=True)

    with pytest.raises((ValueError, TypeError)) as exc_info:
        await WithUuid.create(run_id="not-a-uuid")

    msg = str(exc_info.value)
    if "Invalid UUID for" in msg:
        # Rust-core diagnostic shape (U5)
        assert "withuuid" in msg.lower() or "WithUuid" in msg
        assert "run_id" in msg
        assert "not-a-uuid" in msg
