from typing import Annotated
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from ferro import (
    BackRef,
    Field,
    FerroField,
    ForeignKey,
    ManyToManyField,
    Model,
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


def test_metadata_translation():
    """Verify that Ferro models are correctly translated to SQLAlchemy MetaData."""

    class User(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: Annotated[str, FerroField(unique=True, index=True)]
        is_active: bool = True
        posts: BackRef["Post"] = None

    class Post(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[User, ForeignKey(related_name="posts", on_delete="CASCADE")]

    # Trigger metadata generation
    metadata = get_metadata()

    # Assert User table
    assert "user" in metadata.tables
    user_table = metadata.tables["user"]
    assert isinstance(user_table.c.id.type, sa.Integer)
    assert user_table.c.id.primary_key
    assert user_table.c.username.unique
    assert user_table.c.username.index
    assert isinstance(user_table.c.is_active.type, sa.Boolean)

    # Assert Post table
    assert "post" in metadata.tables
    post_table = metadata.tables["post"]
    assert post_table.c.author_id is not None

    # Check Foreign Key
    fk = list(post_table.c.author_id.foreign_keys)[0]
    assert fk.target_fullname == "user.id"
    assert fk.ondelete == "CASCADE"


def test_foreign_key_unique_true_propagates_to_shadow_column():
    """1:1 relations use ForeignKey(unique=True); Alembic metadata must expose UNIQUE."""

    class Parent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        child: BackRef["Child"] = None

    class Child(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated[
            Parent,
            ForeignKey(related_name="child", unique=True, on_delete="CASCADE"),
        ]

    metadata = get_metadata()
    child_table = metadata.tables["child"]
    assert child_table.columns["parent_id"].unique is True


def test_m2m_translation():
    """Verify that M2M join tables are also translated."""

    class Actor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        movies: Annotated[list["Movie"], ManyToManyField(related_name="actors")] = None

    class Movie(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        actors: BackRef[Actor] = None

    metadata = get_metadata()

    # Join table should exist
    assert "actor_movies" in metadata.tables
    join_table = metadata.tables["actor_movies"]

    assert "actor_id" in join_table.c
    assert "movie_id" in join_table.c

    # Verify FKs on join table
    fks = {fk.target_fullname: fk for fk in join_table.foreign_keys}
    assert "actor.id" in fks
    assert "movie.id" in fks
    assert fks["actor.id"].ondelete == "CASCADE"


def test_uuid_m2m_join_table_uses_uuid_capable_column_types():
    """Join-table FK columns should inherit UUID-capable types from UUID PK models."""

    class UuidTeam(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        members: Annotated[list["UuidMember"], ManyToManyField(related_name="teams")] = None

    class UuidMember(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        email: str
        teams: BackRef[UuidTeam] = None

    metadata = get_metadata()
    join_table = metadata.tables["uuidteam_members"]

    for column_name in ("uuidteam_id", "uuidmember_id"):
        col = join_table.c[column_name]
        assert isinstance(col.type, sa.Uuid) or (
            isinstance(col.type, sa.String) and getattr(col.type, "length", None) == 36
        )


def test_uuid_foreign_key_shadow_column_type():
    """Alembic bridge: UUID PK targets produce a UUID-capable SQLAlchemy type on *_id columns."""

    class UuidAlembicOrg(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str
        members: BackRef[list["UuidAlembicMember"]] = None

    class UuidAlembicMember(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        org: Annotated[UuidAlembicOrg, ForeignKey(related_name="members")]

    metadata = get_metadata()
    member_table = metadata.tables["uuidalembicmember"]
    col = member_table.c.org_id
    assert isinstance(col.type, sa.Uuid) or (
        isinstance(col.type, sa.String) and getattr(col.type, "length", None) == 36
    )


def test_on_delete_translation():
    """Verify that custom on_delete values are respected."""

    class Category(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        products: BackRef["Product"] = None

    class Product(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        category: Annotated[
            Category, ForeignKey(related_name="products", on_delete="SET NULL")
        ]

    metadata = get_metadata()
    product_table = metadata.tables["product"]
    fk = list(product_table.c.category_id.foreign_keys)[0]
    assert fk.ondelete == "SET NULL"
    assert product_table.c.category_id.nullable is True
