import pytest
import uuid
import sqlite3
from contextlib import closing
from typing import Annotated
from ferro import (
    BackRef,
    FerroField,
    ForeignKey,
    ManyToMany,
    Model,
    Relation,
    connect,
    evict_instance,
    execute,
    transaction,
)

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_transaction_using_routes_unqualified_raw_sql(tmp_path):
    """A transaction opened with using=... should pin unqualified work to that connection."""
    app_db = tmp_path / "app.db"
    service_db = tmp_path / "service.db"

    await connect(f"sqlite:{app_db}?mode=rwc", name="app", default=True)
    await connect(f"sqlite:{service_db}?mode=rwc", name="service")

    async with transaction(using="service"):
        await execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")

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
async def test_relationships_loaded_inside_transaction_inherit_transaction(db_url):
    """Relations on transaction-loaded instances should use the active transaction."""

    class TxRelationParent(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str
        children: Relation[list["TxRelationChild"]] = BackRef()

    class TxRelationChild(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str
        parent: Annotated[TxRelationParent, ForeignKey(related_name="children")]

    class TxRelationCourse(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str
        students: Relation[list["TxRelationStudent"]] = BackRef()

    class TxRelationStudent(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str
        courses: Relation[list[TxRelationCourse]] = ManyToMany(related_name="students")

    await connect(db_url, auto_migrate=True)

    parent = await TxRelationParent.create(id=1, label="parent")
    await TxRelationChild.create(id=1, label="child", parent=parent)
    student = await TxRelationStudent.create(id=1, label="student")
    course_a = await TxRelationCourse.create(id=1, label="course-a")
    course_b = await TxRelationCourse.create(id=2, label="course-b")
    await student.courses.add(course_a)

    evict_instance("TxRelationParent", "1")
    evict_instance("TxRelationChild", "1")
    evict_instance("TxRelationStudent", "1")

    async with transaction():
        loaded_parent = await TxRelationParent.get(1)
        assert loaded_parent is not None
        children = await loaded_parent.children.all()
        assert [child.label for child in children] == ["child"]

        loaded_child = await TxRelationChild.get(1)
        assert loaded_child is not None
        loaded_child_parent = await loaded_child.parent
        assert loaded_child_parent.label == "parent"

        loaded_student = await TxRelationStudent.get(1)
        assert loaded_student is not None
        courses = await loaded_student.courses.all()
        assert [course.label for course in courses] == ["course-a"]
        await loaded_student.courses.add(course_b)

    reloaded_student = await TxRelationStudent.get(1)
    assert reloaded_student is not None
    courses = await reloaded_student.courses.order_by(TxRelationCourse.id).all()
    assert [course.label for course in courses] == ["course-a", "course-b"]


@pytest.mark.asyncio
async def test_transaction_commit(db_url):
    """Test that operations inside a transaction are committed on success."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    async with transaction():
        await TxUser.create(username="alice")
        await TxUser.create(username="bob")

    # Verify both exist
    assert await TxUser.where(TxUser.username == "alice").exists()
    assert await TxUser.where(TxUser.username == "bob").exists()


@pytest.mark.asyncio
async def test_transaction_rollback(db_url):
    """Test that operations inside a transaction are rolled back on exception."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    try:
        async with transaction():
            await TxUser.create(username="charlie")
            raise ValueError("Something went wrong!")
    except ValueError:
        pass

    # Verify charlie DOES NOT exist
    assert not await TxUser.where(TxUser.username == "charlie").exists()


@pytest.mark.asyncio
async def test_transaction_atomicity(db_url):
    """Test that if one operation fails, all are rolled back."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    # Create initial user
    await TxUser.create(username="dave")

    try:
        async with transaction():
            # Update dave
            dave = await TxUser.where(TxUser.username == "dave").first()
            dave.username = "dave_updated"
            await dave.save()

            # Create eve
            await TxUser.create(username="eve")

            # Trigger failure
            raise RuntimeError("Abort!")
    except RuntimeError:
        pass

    # dave should still be "dave", eve should not exist
    from ferro import evict_instance

    evict_instance("TxUser", "1")

    dave_check = await TxUser.where(TxUser.username == "dave").first()
    assert dave_check is not None
    assert dave_check.username == "dave"
    assert not await TxUser.where(TxUser.username == "eve").exists()


@pytest.mark.asyncio
async def test_nested_transaction_rolls_back_with_outer(db_url):
    """Nested transaction blocks should not commit independently of the outer transaction."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    try:
        async with transaction():
            await TxUser.create(username="outer")

            async with transaction():
                await TxUser.create(username="inner")

            raise RuntimeError("abort outer")
    except RuntimeError:
        pass

    assert not await TxUser.where(TxUser.username == "outer").exists()
    assert not await TxUser.where(TxUser.username == "inner").exists()


@pytest.mark.asyncio
async def test_bulk_create_participates_in_transaction(db_url):
    """bulk_create should use the active transaction instead of committing independently."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    rows = [TxUser(username="bulk_a"), TxUser(username="bulk_b")]

    try:
        async with transaction():
            inserted = await TxUser.bulk_create(rows)
            assert inserted == 2
            raise RuntimeError("abort bulk transaction")
    except RuntimeError:
        pass

    assert not await TxUser.where(TxUser.username == "bulk_a").exists()
    assert not await TxUser.where(TxUser.username == "bulk_b").exists()


@pytest.mark.asyncio
async def test_nested_transaction_inner_rollback_allows_outer_commit(db_url):
    """An inner rollback should behave like a savepoint, not a separate transaction."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    async with transaction():
        await TxUser.create(username="outer_before")

        try:
            async with transaction():
                await TxUser.create(username="inner")
                raise ValueError("abort inner")
        except ValueError:
            pass

        await TxUser.create(username="outer_after")

    assert await TxUser.where(TxUser.username == "outer_before").exists()
    assert await TxUser.where(TxUser.username == "outer_after").exists()
    assert not await TxUser.where(TxUser.username == "inner").exists()
