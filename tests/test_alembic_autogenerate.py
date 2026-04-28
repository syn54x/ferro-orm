from enum import Enum, StrEnum
from typing import Annotated

import pytest
import sqlalchemy as sa

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from ferro import (
    BackRef,
    FerroField,
    ForeignKey,
    Model,
    Relation,
    clear_registry,
    reset_engine,
)
from ferro.migrations import get_metadata


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()
    yield


def test_nullability_detection():
    """Verify that nullability is correctly detected from Pydantic types."""

    class User(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        email: str  # Required, non-nullable
        bio: str | None = None  # Optional, nullable

    metadata = get_metadata()
    user_table = metadata.tables["user"]

    assert user_table.c.email.nullable is False
    assert user_table.c.bio.nullable is True
    assert user_table.c.id.nullable is False


def test_schema_diff_simulation():
    """
    Simulate a schema change and verify that the metadata reflects the change.
    In a real app, Alembic would compare this metadata against the DB.
    """

    # 1. Initial State
    class Product(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str

    meta_v1 = get_metadata()
    assert "price" not in meta_v1.tables["product"].c

    # 2. Simulate code change (adding a field)
    # We clear the registry and redefine to simulate a fresh run after a code edit
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    clear_registry()

    class Product(Model):  # noqa
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        price: float = 0.0

    meta_v2 = get_metadata()
    assert "price" in meta_v2.tables["product"].c
    assert meta_v2.tables["product"].c.price.nullable is False


def test_index_and_unique_diff():
    """Verify that adding an index/unique flag updates the metadata."""

    class Settings(Model):  # noqa
        id: Annotated[int, FerroField(primary_key=True)]
        key: str

    meta_v1 = get_metadata()
    assert meta_v1.tables["settings"].c.key.unique is False
    assert meta_v1.tables["settings"].c.key.index is False

    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    clear_registry()

    class Settings(Model):  # noqa
        id: Annotated[int, FerroField(primary_key=True)]
        key: Annotated[str, FerroField(unique=True, index=True)]

    meta_v2 = get_metadata()
    assert meta_v2.tables["settings"].c.key.unique is True
    assert meta_v2.tables["settings"].c.key.index is True


def test_enum_generates_with_name():
    """Enum columns must use a named SQLAlchemy Enum so Postgres DDL compiles."""

    class Status(StrEnum):
        DRAFT = "draft"
        ACTIVE = "active"
        ARCHIVED = "archived"

    class Article(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        status: Status

    metadata = get_metadata()
    article_table = metadata.tables["article"]

    assert isinstance(article_table.c.status.type, sa.Enum)
    assert article_table.c.status.type.name is not None
    assert article_table.c.status.type.name == "status"
    assert set(article_table.c.status.type.enums) == {"draft", "active", "archived"}


def test_standard_enum_generates_with_name():
    """Integer-valued Enum columns still get a named type with string labels."""

    class Priority(Enum):
        LOW = 1
        MEDIUM = 2
        HIGH = 3

    class Task(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        priority: Priority

    metadata = get_metadata()
    task_table = metadata.tables["task"]

    assert isinstance(task_table.c.priority.type, sa.Enum)
    assert task_table.c.priority.type.name is not None
    assert task_table.c.priority.type.name == "priority"
    assert set(task_table.c.priority.type.enums) == {"1", "2", "3"}


def test_optional_enum_generates_with_name():
    """Optional Enum columns keep the enum type name and remain nullable."""

    class Color(StrEnum):
        RED = "red"
        GREEN = "green"
        BLUE = "blue"

    class Widget(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        color: Color | None = None

    metadata = get_metadata()
    widget_table = metadata.tables["widget"]

    assert isinstance(widget_table.c.color.type, sa.Enum)
    assert widget_table.c.color.type.name is not None
    assert widget_table.c.color.type.name == "color"
    assert widget_table.c.color.nullable is True
    assert set(widget_table.c.color.type.enums) == {"red", "green", "blue"}


def test_alembic_can_render_enum_for_postgres():
    """Named enums compile on the PostgreSQL dialect without a missing-name error."""

    class Status(StrEnum):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    class Request(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        status: Status
        description: str

    metadata = get_metadata()
    request_table = metadata.tables["request"]

    dialect = postgresql.dialect()
    try:
        sql = str(CreateTable(request_table).compile(dialect=dialect))
    except sa.exc.CompileError as e:
        if "requires a name" in str(e).lower():
            pytest.fail(f"Enum type missing name: {e}")
        raise
    # Postgres references the named enum type on the column (values live in CREATE TYPE elsewhere).
    assert "status" in sql.lower()


def test_foreign_key_index_emits_single_column_index():
    """ForeignKey(index=True) declares a non-unique index on the shadow *_id column."""
    class Org(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        projects: Relation[list["Project"]] = BackRef()

    class Project(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        org: Annotated[Org, ForeignKey(related_name="projects", index=True)]

    metadata = get_metadata()
    project_table = metadata.tables["project"]

    assert project_table.c.org_id.index is True
    assert project_table.c.org_id.unique is False


def test_foreign_key_unique_implies_index_warns():
    """ForeignKey(unique=True, index=True) warns and emits only the unique constraint."""
    class User(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        profile: Relation[list["Profile"]] = BackRef()

    with pytest.warns(UserWarning, match="redundant"):

        class Profile(Model):
            id: Annotated[int, FerroField(primary_key=True)]
            user: Annotated[
                User,
                ForeignKey(related_name="profile", unique=True, index=True),
            ]

    metadata = get_metadata()
    profile_table = metadata.tables["profile"]

    assert profile_table.c.user_id.unique is True
    assert profile_table.c.user_id.index is False


def test_foreign_key_index_default_false():
    """ForeignKey() without index=True keeps the shadow column unindexed."""
    class Org(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        projects: Relation[list["Project"]] = BackRef()

    class Project(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        org: Annotated[Org, ForeignKey(related_name="projects")]

    metadata = get_metadata()
    project_table = metadata.tables["project"]

    assert project_table.c.org_id.index is False


def test_foreign_key_index_with_nullable_fk():
    """ForeignKey(index=True, on_delete='SET NULL') indexes the nullable shadow column."""
    class Org(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        projects: Relation[list["Project"]] = BackRef()

    class Project(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        org: Annotated[
            Org | None,
            ForeignKey(
                related_name="projects",
                on_delete="SET NULL",
                index=True,
            ),
        ] = None

    metadata = get_metadata()
    project_table = metadata.tables["project"]

    assert project_table.c.org_id.index is True
    assert project_table.c.org_id.nullable is True


def test_index_name_matches_rust_runtime_convention_for_fk():
    """Alembic-rendered single-column FK index must use the same name as the Rust runtime emits.

    Cross-emitter DDL parity invariant: every emission path (Alembic autogen, Rust
    runtime DDL, any future emitter) must produce identical index names so that
    autogen runs against an auto_migrate'd database are idempotent.
    """
    class Org(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        projects: Relation[list["Project"]] = BackRef()

    class Project(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        org: Annotated[Org, ForeignKey(related_name="projects", index=True)]

    metadata = get_metadata()
    project_table = metadata.tables["project"]

    fk_indexes = [
        idx
        for idx in project_table.indexes
        if [c.name for c in idx.columns] == ["org_id"]
    ]
    assert len(fk_indexes) == 1, (
        f"Expected exactly one index on org_id, got {len(fk_indexes)}: "
        f"{[i.name for i in project_table.indexes]}"
    )
    assert fk_indexes[0].name == "idx_project_org_id", (
        f"Alembic must emit idx_project_org_id to match Rust runtime DDL; got "
        f"{fk_indexes[0].name!r}"
    )


def test_index_name_matches_rust_runtime_convention_for_ferro_field():
    """FerroField(index=True) on a plain column must also use the cross-emitter idx_* name."""

    class Settings(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        key: Annotated[str, FerroField(index=True)]

    metadata = get_metadata()
    settings_table = metadata.tables["settings"]

    indexes = [
        idx
        for idx in settings_table.indexes
        if [c.name for c in idx.columns] == ["key"]
    ]
    assert len(indexes) == 1, (
        f"Expected exactly one index on key, got {len(indexes)}: "
        f"{[i.name for i in settings_table.indexes]}"
    )
    assert indexes[0].name == "idx_settings_key", (
        f"Alembic must emit idx_settings_key to match Rust runtime DDL; got "
        f"{indexes[0].name!r}"
    )
