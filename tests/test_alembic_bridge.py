from typing import Annotated
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from ferro import (
    BackRef,
    Field,
    FerroField,
    ForeignKey,
    ManyToMany,
    Model,
    Relation,
    clear_registry,
    reset_engine,
)
from ferro.migrations.alembic import _build_sa_table, _map_to_sa_type
from ferro.migrations import get_metadata
from ferro.schema_metadata import build_model_schema


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
        posts: Relation[list["Post"]] = BackRef()

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


def test_legacy_json_table_builder_emits_deprecation_warning():
    md = sa.MetaData()
    schema = {
        "properties": {
            "id": {"type": "integer", "primary_key": True, "autoincrement": True},
            "name": {"type": "string"},
        }
    }
    with pytest.deprecated_call(
        match="_build_sa_table\\(\\) is deprecated.*Planned removal in v0\\.14\\.0"
    ):
        _build_sa_table(md, "legacydoc", schema, model_cls=None)
    assert "legacydoc" in md.tables


def test_legacy_map_to_sa_type_emits_deprecation_warning():
    schema = {"properties": {}}
    col_info = {"type": "string"}
    with pytest.deprecated_call(
        match="_map_to_sa_type\\(\\) is deprecated.*Planned removal in v0\\.14\\.0"
    ):
        resolved = _map_to_sa_type(schema, col_info, "legacy_field")
    assert isinstance(resolved, sa.String)


def test_foreign_key_unique_true_propagates_to_shadow_column():
    """1:1 relations use ForeignKey(unique=True); Alembic metadata must expose UNIQUE."""

    class Parent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        child: "Child" = BackRef()

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
        movies: Relation[list["Movie"]] = ManyToMany(related_name="actors")

    class Movie(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        actors: Relation[list["Actor"]] = BackRef()

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
        members: Relation[list["UuidMember"]] = ManyToMany(related_name="teams")

    class UuidMember(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        email: str
        teams: Relation[list["UuidTeam"]] = BackRef()

    metadata = get_metadata()
    join_table = metadata.tables["uuidteam_members"]

    for column_name in ("uuidteam_id", "uuidmember_id"):
        col = join_table.c[column_name]
        assert isinstance(col.type, sa.Uuid) or (
            isinstance(col.type, sa.String) and getattr(col.type, "length", None) == 36
        )
        assert col.nullable is False


def test_uuid_foreign_key_shadow_column_type():
    """Alembic bridge: UUID PK targets produce a UUID-capable SQLAlchemy type on *_id columns."""

    class UuidAlembicOrg(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str
        members: Relation[list["UuidAlembicMember"]] = BackRef()

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
        products: Relation[list["Product"]] = BackRef()

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


def test_explicit_foreign_key_shadow_id_no_duplicate_alembic_columns():
    """Declaring ``{relation}_id`` for static checkers must not duplicate DDL columns.

    Ferro injects a shadow ``*_id`` for every :class:`~ferro.base.ForeignKey`. Some
    type checkers (for example Ty) do not see metaclass-injected fields, so users may
    duplicate the declaration in the class body. That must still produce exactly one
    JSON-schema property and one SQLAlchemy column for Alembic autogenerate.
    """

    class TyShadowFkJobRole(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        scorecards: Relation[list["TyShadowFkScorecard"]] = BackRef()

    class TyShadowFkScorecard(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        job_role: Annotated[TyShadowFkJobRole, ForeignKey(related_name="scorecards")]
        job_role_id: int | None = None

    assert list(TyShadowFkScorecard.model_fields).count("job_role_id") == 1

    schema = build_model_schema(TyShadowFkScorecard)
    props = schema["properties"]
    assert "job_role_id" in props
    assert sum(1 for k in props if k == "job_role_id") == 1
    fk_meta = props["job_role_id"].get("foreign_key") or {}
    assert fk_meta.get("to_table") == "tyshadowfkjobrole"

    metadata = get_metadata()
    tbl = metadata.tables["tyshadowfkscorecard"]
    assert list(tbl.columns.keys()).count("job_role_id") == 1
    col = tbl.c.job_role_id
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].target_fullname == "tyshadowfkjobrole.id"


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_explicit_foreign_key_shadow_id_auto_migrate_roundtrip(db_url):
    """Runtime migrate + ORM must treat explicit ``*_id`` as the single FK column."""

    from ferro import connect

    class TyRoundJobRole(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        scorecards: Relation[list["TyRoundScorecard"]] = BackRef()

    class TyRoundScorecard(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        job_role: Annotated[TyRoundJobRole, ForeignKey(related_name="scorecards")]
        job_role_id: int | None = None

    await connect(db_url, auto_migrate=True)

    role = await TyRoundJobRole.create(name="ic")
    card = await TyRoundScorecard.create(title="card-a", job_role=role)
    assert card.job_role_id == role.id

    by_attr = await TyRoundScorecard.where(
        TyRoundScorecard.job_role_id == role.id
    ).first()
    assert by_attr is not None and by_attr.id == card.id

    by_lambda = await TyRoundScorecard.where(
        lambda s: s.job_role_id == role.id
    ).first()
    assert by_lambda is not None and by_lambda.id == card.id
