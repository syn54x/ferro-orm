"""Operation-seam registration sync: single-flight, zero-DDL, clean path (#247)."""

import asyncio
from typing import Annotated

import pytest

import ferro
from ferro import Model, OperationalError, connect, execute
from ferro._core import _bulk_install_count_for_test
from ferro.base import FerroField
from ferro import state as ferro_state


@pytest.mark.asyncio
async def test_late_model_after_connect_save_and_query_without_ddl(
    db_url, clean_registry
) -> None:
    """A model defined after connect syncs on first operation — no create_tables."""

    class OsEarly(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)

    async with ferro.engines.session():
        await execute(
            'CREATE TABLE IF NOT EXISTS "oslate" ( '
            '"id" integer PRIMARY KEY AUTOINCREMENT NOT NULL, '
            '"note" varchar NOT NULL )'
        )

    class OsLate(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        note: str

    async with ferro.engines.session():
        row = await OsLate.create(note="synced")
        assert (await OsLate.get(row.id)).note == "synced"
        assert len(await OsLate.all()) == 1


@pytest.mark.asyncio
async def test_operation_seam_sync_does_not_create_tables(db_url, clean_registry) -> None:
    """Registration sync on save must not emit DDL when the table is missing."""

    class OsSeeded(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        tag: str

    await connect(db_url, auto_migrate=True)

    class OsMissing(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: str

    instance = OsMissing(value="nope")
    async with ferro.engines.session():
        with pytest.raises(OperationalError):
            await instance.save()


@pytest.mark.asyncio
async def test_concurrent_first_operations_single_bulk_install(
    db_url, clean_registry
) -> None:
    """Concurrent dirty-path ops serialize to exactly one bulk install."""

    class OsBase(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)

    async with ferro.engines.session():
        await execute(
            'CREATE TABLE IF NOT EXISTS "osconcurrent" ( '
            '"id" integer PRIMARY KEY AUTOINCREMENT NOT NULL, '
            '"score" integer NOT NULL )'
        )

    class OsConcurrent(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        score: int

    baseline = _bulk_install_count_for_test()

    async def first_save(score: int) -> None:
        async with ferro.engines.session():
            await OsConcurrent.create(score=score)

    await asyncio.gather(*(first_save(i) for i in range(8)))
    assert _bulk_install_count_for_test() - baseline == 1


@pytest.mark.asyncio
async def test_clean_path_skips_bulk_install_and_push(
    db_url, clean_registry, monkeypatch
) -> None:
    """After sync, clean operations compare generation counters only — no FFI push."""

    class OsClean(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)

    class OsCleanLate(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        note: str

    async with ferro.engines.session():
        await execute(
            'CREATE TABLE IF NOT EXISTS "oscleanlate" ( '
            '"id" integer PRIMARY KEY AUTOINCREMENT NOT NULL, '
            '"note" varchar NOT NULL )'
        )
        row = await OsCleanLate.create(note="warm")
        pk = row.id

    push_calls: list[object] = []
    real_push = ferro._push_registration_to_rust

    def tracking_push(*args, **kwargs):
        push_calls.append((args, kwargs))
        return real_push(*args, **kwargs)

    monkeypatch.setattr(ferro, "_push_registration_to_rust", tracking_push)

    bulk_after_warm = _bulk_install_count_for_test()
    push_calls.clear()

    async with ferro.engines.session():
        await OsCleanLate.all()
        await OsCleanLate.get(pk)

    assert push_calls == []
    assert _bulk_install_count_for_test() == bulk_after_warm


@pytest.mark.asyncio
async def test_failed_resolve_stays_dirty_and_retries(
    db_url, clean_registry, monkeypatch
) -> None:
    """A failed resolve leaves the registry dirty; the next operation retries."""

    class OsRetry(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: str

    await connect(db_url, auto_migrate=True)

    class OsRetryLate(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        payload: str

    async with ferro.engines.session():
        await execute(
            'CREATE TABLE IF NOT EXISTS "osretrylate" ( '
            '"id" integer PRIMARY KEY AUTOINCREMENT NOT NULL, '
            '"payload" varchar NOT NULL )'
        )

    from ferro.relations import resolve_relationships

    real_resolve = resolve_relationships
    attempts = 0

    def flaky_resolve() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated resolve failure")
        return real_resolve()

    monkeypatch.setattr("ferro.relations.resolve_relationships", flaky_resolve)

    async with ferro.engines.session():
        with pytest.raises(RuntimeError, match="simulated resolve failure"):
            await OsRetryLate.create(payload="first")
    assert ferro_state.is_modelset_dirty()

    async with ferro.engines.session():
        row = await OsRetryLate.create(payload="second")
        assert (await OsRetryLate.get(row.id)).payload == "second"
    assert not ferro_state.is_modelset_dirty()


def test_alembic_metadata_uses_ensure_resolved_not_operation_seam(
    clean_registry, monkeypatch
) -> None:
    """Alembic autogenerate keeps ensure_resolved_modelset; ops must not change it."""

    class OsAlembic(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str

    ensure_calls: list[str] = []
    operation_calls: list[str] = []
    real_ensure = ferro.ensure_resolved_modelset

    def tracking_ensure() -> dict:
        ensure_calls.append("ensure")
        return real_ensure()

    async def tracking_operation() -> None:
        operation_calls.append("operation")

    monkeypatch.setattr(ferro, "ensure_resolved_modelset", tracking_ensure)
    monkeypatch.setattr(
        ferro, "_ensure_rust_registration_synced_for_operation", tracking_operation
    )

    from ferro.migrations.alembic import get_metadata

    metadata = get_metadata()
    assert "osalembic" in metadata.tables
    assert ensure_calls == ["ensure"]
    assert operation_calls == []
