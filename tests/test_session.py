import asyncio
import warnings
from contextlib import AsyncExitStack
from typing import Annotated

import pytest

import ferro


pytestmark = pytest.mark.sqlite_only


class SessionMarker(ferro.Model):
    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    label: str


@pytest.fixture(autouse=True)
def _ensure_models_registered():
    from ferro.state import _MODEL_REGISTRY_PY

    SessionMarker._reregister_ferro()
    _MODEL_REGISTRY_PY[SessionMarker.__name__] = SessionMarker
    yield


@pytest.mark.asyncio
async def test_session_query_api_routes_to_bound_connection(tmp_path):
    app_db = tmp_path / "app.db"
    analytics_db = tmp_path / "analytics.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{analytics_db}?mode=rwc", name="analytics")
    await ferro.create_tables()
    await ferro.create_tables(using="analytics")

    async with ferro.engines.session("analytics") as session:
        created = await SessionMarker.create(id=1, label="analytics")
        fetched = await session.query(SessionMarker).where(lambda t: t.id == 1).first()

    assert created.id == 1
    assert fetched is not None
    assert fetched.label == "analytics"

    with pytest.warns(
        DeprecationWarning, match="Implicit default-connection routing.*v0\\.14\\.0"
    ):
        default_rows = await SessionMarker.all()
    assert default_rows == []


@pytest.mark.asyncio
async def test_nested_sessions_shadow_and_restore(tmp_path):
    app_db = tmp_path / "app.db"
    analytics_db = tmp_path / "analytics.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{analytics_db}?mode=rwc", name="analytics")
    await ferro.create_tables()
    await ferro.create_tables(using="analytics")

    async with ferro.engines.session("app"):
        await SessionMarker.create(id=1, label="app")
        async with ferro.engines.session("analytics"):
            await SessionMarker.create(id=1, label="analytics")
            inner_rows = await SessionMarker.all()
            assert [row.label for row in inner_rows] == ["analytics"]
        outer_rows = await SessionMarker.all()
        assert [row.label for row in outer_rows] == ["app"]


@pytest.mark.asyncio
async def test_explicit_session_override_beats_ambient(tmp_path):
    app_db = tmp_path / "app.db"
    analytics_db = tmp_path / "analytics.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{analytics_db}?mode=rwc", name="analytics")
    await ferro.create_tables()
    await ferro.create_tables(using="analytics")

    async with ferro.engines.session("app") as app_session:
        await SessionMarker.create(id=1, label="app")
        async with ferro.engines.session("analytics"):
            await SessionMarker.create(id=1, label="analytics")
            rows = await SessionMarker.all(session=app_session)
            assert [row.label for row in rows] == ["app"]


@pytest.mark.asyncio
async def test_unnamed_session_binds_default_connection(tmp_path):
    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", auto_migrate=True)

    async with ferro.engines.session() as session:
        assert session.connection_name == "default"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            exists = await SessionMarker.where(
                lambda t: t.label == "missing"
            ).exists()
            created = await SessionMarker.create(id=20, label="session-bound")

        session_warnings = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "without an active session" in str(w.message)
        ]
        assert not session_warnings
        assert exists is False
        assert created.id == 20


@pytest.mark.asyncio
async def test_legacy_unqualified_operation_warns(tmp_path):
    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.create_tables()

    with pytest.warns(
        DeprecationWarning, match="Implicit default-connection routing.*v0\\.14\\.0"
    ):
        created = await SessionMarker.create(id=10, label="legacy")
    with pytest.warns(
        DeprecationWarning, match="Implicit default-connection routing.*v0\\.14\\.0"
    ):
        loaded = await SessionMarker.get(10)

    assert created.id == 10
    assert loaded.label == "legacy"


@pytest.mark.asyncio
async def test_session_exit_from_different_asyncio_context_succeeds(tmp_path):
    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", auto_migrate=True)

    stack = AsyncExitStack()
    await stack.__aenter__()
    await stack.enter_async_context(ferro.engines.session("default"))
    await SessionMarker.create(id=1, label="demo")

    async def close_stack() -> None:
        await stack.__aexit__(None, None, None)

    await asyncio.create_task(close_stack())

    with pytest.raises(RuntimeError, match="Session is closed.*Open a new session"):
        await SessionMarker.all()


@pytest.mark.asyncio
async def test_session_close_from_different_context_succeeds(tmp_path):
    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", auto_migrate=True)

    session = ferro.engines.session("default")
    await session.__aenter__()
    await SessionMarker.create(id=2, label="demo")

    async def close_session() -> None:
        await session.close()

    await asyncio.create_task(close_session())
    assert session.session_id is None

    with pytest.raises(RuntimeError, match="Session is closed.*Open a new session"):
        await SessionMarker.all()


@pytest.mark.asyncio
async def test_session_close_is_idempotent(tmp_path):
    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", auto_migrate=True)

    session = ferro.engines.session("default")
    await session.__aenter__()
    await session.close()
    await session.close()
    assert session.session_id is None


@pytest.mark.asyncio
async def test_nested_session_cross_context_close_inner_then_outer(tmp_path):
    app_db = tmp_path / "app.db"
    analytics_db = tmp_path / "analytics.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{analytics_db}?mode=rwc", name="analytics")
    await ferro.create_tables()
    await ferro.create_tables(using="analytics")

    outer = ferro.engines.session("app")
    inner = ferro.engines.session("analytics")
    await outer.__aenter__()
    await SessionMarker.create(id=1, label="app")
    await inner.__aenter__()
    await SessionMarker.create(id=1, label="analytics")

    async def close_inner() -> None:
        await inner.close()

    await asyncio.create_task(close_inner())

    with pytest.raises(RuntimeError, match="Session is closed.*Open a new session"):
        await SessionMarker.all()

    rows = await SessionMarker.all(session=outer)
    assert [row.label for row in rows] == ["app"]

    async def close_outer() -> None:
        await outer.close()

    await asyncio.create_task(close_outer())
    assert outer.session_id is None

    with pytest.raises(RuntimeError, match="Session is closed.*Open a new session"):
        await SessionMarker.all(session=outer)


@pytest.mark.asyncio
async def test_ambient_operations_after_session_close_fail_with_session_closed_error(
    tmp_path,
):
    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", auto_migrate=True)

    session = ferro.engines.session("default")
    await session.__aenter__()
    await SessionMarker.create(id=3, label="demo")

    async def close_session() -> None:
        await session.close()

    await asyncio.create_task(close_session())

    with pytest.raises(RuntimeError, match="Session is closed.*Open a new session"):
        await SessionMarker.all()


@pytest.mark.asyncio
async def test_explicit_session_query_after_close_fails_with_session_closed_error(
    tmp_path,
):
    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", auto_migrate=True)

    session = ferro.engines.session("default")
    await session.__aenter__()
    await session.close()

    with pytest.raises(RuntimeError, match="Session is closed.*Open a new session"):
        await session.query(SessionMarker).where(lambda t: t.id == 1).first()


@pytest.mark.asyncio
async def test_same_context_exit_when_ambient_session_replaced_raises_clear_error(
    tmp_path,
):
    from ferro.state import _CURRENT_SESSION

    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.create_tables()

    outer = ferro.engines.session("app")
    inner = ferro.engines.session("analytics")
    await outer.__aenter__()
    _CURRENT_SESSION.set(inner)

    with pytest.raises(RuntimeError, match="ambient session does not match"):
        await outer.close()

    assert outer.session_id is not None
    assert _CURRENT_SESSION.get() is inner


@pytest.mark.asyncio
async def test_same_context_outer_close_after_inner_cross_context_close_raises(
    tmp_path,
):
    app_db = tmp_path / "app.db"
    analytics_db = tmp_path / "analytics.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{analytics_db}?mode=rwc", name="analytics")
    await ferro.create_tables()
    await ferro.create_tables(using="analytics")

    outer = ferro.engines.session("app")
    inner = ferro.engines.session("analytics")
    await outer.__aenter__()
    await inner.__aenter__()

    async def close_inner() -> None:
        await inner.close()

    await asyncio.create_task(close_inner())

    with pytest.raises(RuntimeError, match="ambient session does not match"):
        await outer.close()

    assert outer.session_id is not None


@pytest.mark.asyncio
async def test_stale_ambient_with_using_still_raises_session_closed(tmp_path):
    app_db = tmp_path / "app.db"
    analytics_db = tmp_path / "analytics.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{analytics_db}?mode=rwc", name="analytics")
    await ferro.create_tables()
    await ferro.create_tables(using="analytics")

    session = ferro.engines.session("app")
    await session.__aenter__()

    async def close_session() -> None:
        await session.close()

    await asyncio.create_task(close_session())

    with pytest.raises(RuntimeError, match="Session is closed.*Open a new session"):
        await SessionMarker.all(using="analytics")


@pytest.mark.asyncio
async def test_transaction_after_cross_context_close_raises(tmp_path):
    app_db = tmp_path / "app.db"
    await ferro.connect(f"sqlite:{app_db}?mode=rwc", auto_migrate=True)

    session = ferro.engines.session("default")
    await session.__aenter__()

    async def close_session() -> None:
        await session.close()

    await asyncio.create_task(close_session())

    with pytest.raises(RuntimeError, match="Session is closed.*Open a new session"):
        async with ferro.transaction():
            pass
