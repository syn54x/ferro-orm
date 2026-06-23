import warnings
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
