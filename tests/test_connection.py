import sqlite3
from contextlib import closing
from typing import Annotated

import pytest

import ferro

pytestmark = pytest.mark.backend_matrix


class ConnectionRouteMarker(ferro.Model):
    id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
    label: str | None = None


@pytest.fixture(autouse=True)
def _ensure_models_registered():
    from ferro.state import _MODEL_REGISTRY_PY

    ConnectionRouteMarker._reregister_ferro()
    _MODEL_REGISTRY_PY[ConnectionRouteMarker.__name__] = ConnectionRouteMarker
    yield


@pytest.mark.asyncio
async def test_connection_smoke(db_url):
    """Test connecting to the configured backend."""
    await ferro.connect(db_url)


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_named_connection_rejects_duplicate_name(tmp_path):
    """A named connection cannot be silently replaced by a second registration."""
    app_db = tmp_path / "app.db"
    other_db = tmp_path / "other.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)

    with pytest.raises(ValueError, match="already registered"):
        await ferro.connect(f"sqlite:{other_db}?mode=rwc", name="app")


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_set_default_connection_routes_unqualified_operations(tmp_path):
    """Changing the default connection routes legacy unqualified operations."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")

    ferro.set_default_connection("service")
    await ferro.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")

    with closing(sqlite3.connect(app_db)) as app_conn:
        app_tables = app_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'marker'"
        ).fetchall()
    with closing(sqlite3.connect(service_db)) as service_conn:
        service_tables = service_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'marker'"
        ).fetchall()

    assert app_tables == []
    assert service_tables == [("marker",)]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_raw_execute_using_routes_to_named_connection(tmp_path):
    """Raw SQL can target a named connection without changing the default."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")

    await ferro.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)", using="service")

    with closing(sqlite3.connect(app_db)) as app_conn:
        app_tables = app_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'marker'"
        ).fetchall()
    with closing(sqlite3.connect(service_db)) as service_conn:
        service_tables = service_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'marker'"
        ).fetchall()

    assert app_tables == []
    assert service_tables == [("marker",)]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_create_tables_using_routes_schema_to_named_connection(tmp_path):
    """Manual schema creation should run against the selected connection."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")

    await ferro.create_tables(using="service")

    table_name = "connectionroutemarker"
    with closing(sqlite3.connect(app_db)) as app_conn:
        app_tables = app_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchall()
    with closing(sqlite3.connect(service_db)) as service_conn:
        service_tables = service_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchall()

    assert app_tables == []
    assert service_tables == [(table_name,)]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_model_using_routes_create_and_query_to_named_connection(tmp_path):
    """The ORM routing surface should write/read through the selected connection."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    created = await ConnectionRouteMarker.using("service").create(id=42)
    rows = await ConnectionRouteMarker.using("service").all()

    assert created.id == 42
    assert [row.id for row in rows] == [42]
    assert await ConnectionRouteMarker.all() == []


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_model_using_routes_query_mutations_to_named_connection(tmp_path):
    """Connection-bound queries should count, update, and delete on that connection."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    await ConnectionRouteMarker.create(id=100, label="app")
    await ConnectionRouteMarker.using("service").create(id=1, label="service")
    await ConnectionRouteMarker.using("service").create(id=2, label="service-delete")

    service_query = ConnectionRouteMarker.using("service")

    assert await service_query.select().count() == 2
    assert await service_query.where(ConnectionRouteMarker.label == "service").exists()

    updated = await service_query.where(ConnectionRouteMarker.id == 1).update(
        label="service-updated"
    )
    deleted = await service_query.where(ConnectionRouteMarker.id == 2).delete()

    assert updated == 1
    assert deleted == 1
    assert [(row.id, row.label) for row in await service_query.all()] == [
        (1, "service-updated")
    ]
    assert [(row.id, row.label) for row in await ConnectionRouteMarker.all()] == [
        (100, "app")
    ]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_model_using_routes_helper_writes_to_named_connection(tmp_path):
    """Connection-bound helper writes should stay on the selected connection."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    service_model = ConnectionRouteMarker.using("service")

    inserted = await service_model.bulk_create(
        [ConnectionRouteMarker(id=10, label="bulk")]
    )
    created_row, created = await service_model.get_or_create(
        defaults={"label": "created"}, id=11
    )
    updated_row, updated_created = await service_model.update_or_create(
        defaults={"label": "updated"}, id=10
    )

    assert inserted == 1
    assert created is True
    assert created_row.label == "created"
    assert updated_created is False
    assert updated_row.label == "updated"
    assert [(row.id, row.label) for row in await service_model.select().order_by(ConnectionRouteMarker.id).all()] == [
        (10, "updated"),
        (11, "created"),
    ]
    assert await ConnectionRouteMarker.all() == []


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_identity_map_is_scoped_by_named_connection(tmp_path):
    """Same model and PK loaded through different connections should not share objects."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    await ConnectionRouteMarker.create(id=1, label="app")
    await ConnectionRouteMarker.using("service").create(id=1, label="service")

    app_row = await ConnectionRouteMarker.get(1)
    service_row = await ConnectionRouteMarker.using("service").get(1)

    assert app_row is not None
    assert service_row is not None
    assert app_row is not service_row
    assert app_row.label == "app"
    assert service_row.label == "service"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_service_loaded_instance_save_uses_origin_connection(tmp_path):
    """Instance methods should prefer the connection that hydrated the object."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    await ConnectionRouteMarker.create(id=1, label="app")
    await ConnectionRouteMarker.using("service").create(id=1, label="service")

    service_row = await ConnectionRouteMarker.using("service").get(1)
    assert service_row is not None

    service_row.label = "service-saved"
    await service_row.save()

    app_row = await ConnectionRouteMarker.get(1)
    reloaded_service_row = await ConnectionRouteMarker.using("service").get(1)

    assert app_row is not None
    assert reloaded_service_row is not None
    assert app_row.label == "app"
    assert reloaded_service_row.label == "service-saved"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_service_loaded_instance_delete_uses_origin_connection(tmp_path):
    """Instance delete should prefer the connection that hydrated the object."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    await ConnectionRouteMarker.create(id=1, label="app")
    await ConnectionRouteMarker.using("service").create(id=1, label="service")

    service_row = await ConnectionRouteMarker.using("service").get(1)
    assert service_row is not None

    await service_row.delete()

    assert await ConnectionRouteMarker.get(1) is not None
    assert await ConnectionRouteMarker.using("service").get(1) is None


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_service_loaded_instance_refresh_uses_origin_connection(tmp_path):
    """Instance refresh should reload from the connection that hydrated the object."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    await ConnectionRouteMarker.create(id=1, label="app")
    await ConnectionRouteMarker.using("service").create(id=1, label="service")

    service_row = await ConnectionRouteMarker.using("service").get(1)
    assert service_row is not None

    await ferro.execute(
        "UPDATE connectionroutemarker SET label = ? WHERE id = ?",
        "service-refreshed",
        1,
        using="service",
    )
    await service_row.refresh()

    assert service_row.label == "service-refreshed"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_backref_query_inherits_source_instance_origin(tmp_path):
    """Relationship queries should inherit the source object's origin connection."""

    class RouteParent(ferro.Model):
        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        label: str
        children: ferro.Relation[list["RouteChild"]] = ferro.BackRef()

    class RouteChild(ferro.Model):
        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        label: str
        parent: Annotated[RouteParent, ferro.ForeignKey(related_name="children")]

    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    app_parent = await RouteParent.create(id=1, label="app-parent")
    service_parent = await RouteParent.using("service").create(
        id=1, label="service-parent"
    )
    await RouteChild.create(id=1, label="app-child", parent=app_parent)
    await RouteChild.using("service").create(
        id=1, label="service-child", parent=service_parent
    )

    loaded_service_parent = await RouteParent.using("service").get(1)
    assert loaded_service_parent is not None

    children = await loaded_service_parent.children.all()

    assert [child.label for child in children] == ["service-child"]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_forward_fk_load_inherits_source_instance_origin(tmp_path):
    """Forward FK lazy loads should inherit the source object's origin connection."""

    class RouteForwardParent(ferro.Model):
        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        label: str
        children: ferro.Relation[list["RouteForwardChild"]] = ferro.BackRef()

    class RouteForwardChild(ferro.Model):
        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        label: str
        parent: Annotated[
            RouteForwardParent, ferro.ForeignKey(related_name="children")
        ]

    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    app_parent = await RouteForwardParent.create(id=1, label="app-parent")
    service_parent = await RouteForwardParent.using("service").create(
        id=1, label="service-parent"
    )
    await RouteForwardChild.create(id=1, label="app-child", parent=app_parent)
    await RouteForwardChild.using("service").create(
        id=1, label="service-child", parent=service_parent
    )

    service_child = await RouteForwardChild.using("service").get(1)
    assert service_child is not None

    parent = await service_child.parent

    assert parent is not None
    assert parent.label == "service-parent"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_m2m_relation_writes_inherit_source_instance_origin(tmp_path):
    """M2M relation writes should inherit the source object's origin connection."""

    class RouteStudent(ferro.Model):
        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        label: str
        courses: ferro.Relation[list["RouteCourse"]] = ferro.ManyToMany(
            related_name="students"
        )

    class RouteCourse(ferro.Model):
        id: Annotated[int | None, ferro.FerroField(primary_key=True)] = None
        label: str
        students: ferro.Relation[list[RouteStudent]] = ferro.BackRef()

    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await ferro.connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await ferro.connect(f"sqlite:{service_db}?mode=rwc", name="service")
    await ferro.create_tables()
    await ferro.create_tables(using="service")

    app_student = await RouteStudent.create(id=1, label="app-student")
    app_course = await RouteCourse.create(id=1, label="app-course")
    service_student = await RouteStudent.using("service").create(
        id=1, label="service-student"
    )
    service_course = await RouteCourse.using("service").create(
        id=1, label="service-course"
    )

    await service_student.courses.add(service_course)

    assert [course.label for course in await service_student.courses.all()] == [
        "service-course"
    ]
    assert [course.label for course in await app_student.courses.all()] == []
    assert [student.label for student in await app_course.students.all()] == []


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_pool_config_rejects_min_connections_above_max(tmp_path):
    """Pool configuration should validate when it is constructed."""
    with pytest.raises(ValueError, match="min_connections"):
        ferro.PoolConfig(max_connections=1, min_connections=2)


@pytest.mark.asyncio
async def test_connect_passes_pool_config_to_core(monkeypatch):
    """Validated pool configuration should cross the Python/Rust API boundary."""
    calls = []

    async def fake_core_connect(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(ferro, "_core_connect", fake_core_connect)

    await ferro.connect(
        "sqlite::memory:",
        pool=ferro.PoolConfig(max_connections=3, min_connections=1),
    )

    assert calls == [
        (
            ("sqlite::memory:",),
            {
                "auto_migrate": False,
                "name": None,
                "default": False,
                "max_connections": 3,
                "min_connections": 1,
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_unqualified_operation_requires_default_connection(tmp_path):
    """A named non-default connection should not be guessed for legacy operations."""
    db_file = tmp_path / "app.db"

    await ferro.connect(f"sqlite:{db_file}?mode=rwc", name="app")

    with pytest.raises(RuntimeError, match="No default connection selected"):
        await ferro.execute("SELECT 1")


@pytest.mark.asyncio
async def test_invalid_connection_string():
    """Test that invalid connection strings raise the appropriate error."""
    with pytest.raises(Exception) as excinfo:
        await ferro.connect("nonexistent_db://localhost")

    # The error should come from Rust/SQLx
    assert "DB Connection failed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_unsupported_database_scheme_is_rejected_before_connect_attempt():
    """Unsupported schemes should fail classification before any DB driver connect attempt."""
    with pytest.raises(Exception) as excinfo:
        await ferro.connect("mysql://user:pass@localhost/db")

    assert "Unsupported database URL scheme" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_postgres_connection(db_url):
    """Test connecting to the configured Postgres backend."""
    await ferro.connect(db_url)
