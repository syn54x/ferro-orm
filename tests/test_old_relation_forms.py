"""Tests of the relation declarations the explicit foreign-key column replaces.

Each test here exercises an old form itself (`ForeignKey`, `BackRef`, an
explicit `<field>_id` beside a `ForeignKey`), so it has no new form to convert
to. They moved here unchanged in name and body from the files that now declare
their models with `Field(foreign_key=...)`, and this file goes whole when the
old forms are removed.
"""

from typing import Annotated

import pytest

from ferro import (
    BackRef,
    FerroField,
    Field,
    ForeignKey,
    ManyToMany,
    Model,
    Relation,
    clear_registry,
    reset_engine,
)
from ferro.migrations import get_metadata


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.registry import REGISTRY

    REGISTRY.reset_for_test()
    reset_engine()
    clear_registry()
    yield


# -- from tests/test_alembic_autogenerate.py ------------------------------------------


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

    # Only the unique artifact survives; the redundant index is dropped.
    index_names = {idx.name: idx.unique for idx in profile_table.indexes}
    assert index_names == {"uq_profile_user_id": True}


# -- from tests/test_alembic_bridge.py ---------------------------------------------------


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

    specs = TyShadowFkScorecard.__ferro_columns__
    assert "job_role_id" in specs
    assert sum(1 for k in specs if k == "job_role_id") == 1
    fk = specs["job_role_id"].foreign_key
    assert fk is not None and fk.to_table == "tyshadowfkjobrole"

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

    from ferro import connect, engines

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
    async with engines.session():
        role = await TyRoundJobRole.create(name="ic")
        card = await TyRoundScorecard.create(title="card-a", job_role_id=role.id)
        assert card.job_role_id == role.id

        by_shadow_id = await TyRoundScorecard.where(
            lambda s: s.job_role_id == role.id
        ).first()
        assert by_shadow_id is not None and by_shadow_id.id == card.id


# -- from tests/test_alembic_nullability.py


def test_on_delete_set_null_infers_nullable_shadow_fk():
    class Parent(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        children: Relation[list["ChildSetNull"]] = BackRef()

    class ChildSetNull(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        parent: Annotated[
            Parent,
            ForeignKey(related_name="children", on_delete="SET NULL"),
        ]

    t = get_metadata().tables["childsetnull"]
    assert t.c.parent_id.nullable is True


# -- from tests/test_composite_unique.py


def test_m2m_reverse_index_lives_on_forward_side():
    """D3: passing reverse_index= to BackRef() is a TypeError."""
    with pytest.raises(TypeError):
        BackRef(reverse_index=True)


def test_m2m_self_referential_reverse_index():
    """D7: self-referential M2M gets a reverse index on synthesized join table."""

    class UserSelfRef(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        following: Relation[list["UserSelfRef"]] = ManyToMany(related_name="followers")
        followers: Relation[list["UserSelfRef"]] = BackRef()

    metadata = get_metadata()
    join_tables = [
        (name, t)
        for name, t in metadata.tables.items()
        if name != "userselfref" and "userselfref" in name
    ]
    assert join_tables, f"no join table found for self-ref M2M: {list(metadata.tables)}"
    _, join = join_tables[0]
    non_unique = [i for i in join.indexes if not i.unique]
    assert len(non_unique) == 1
