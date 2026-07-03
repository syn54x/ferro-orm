"""FF-B B3/B4: artifact naming is single-sourced in ``ferro-ddl-lowering``.

The IR compiler and the Alembic bridge consume the Rust name builders over
``_core`` FFI; no Python code re-implements the name formats or the 63-char
truncation guards. These tests pin (a) the FFI surface itself, (b) that the
compiler's IR names are the FFI names (including for >63-char identifiers,
where the old hand-rolled Python helpers drifted from Rust), and (c) that the
Alembic bridge renders named FK constraints and standalone named unique
indexes — the artifact shapes both emitters share.
"""

from typing import Annotated, ClassVar, ForwardRef

import pytest
import sqlalchemy as sa

from ferro import (
    BackRef,
    FerroField,
    ForeignKey,
    Model,
    Relation,
    clear_registry,
    reset_engine,
)
from ferro._core import (
    _ddl_check_constraint_name,
    _ddl_composite_index_name,
    _ddl_composite_unique_name,
    _ddl_fk_name,
    _ddl_single_index_name,
    _ddl_single_unique_name,
)
from ferro.ir import compile_registry_schema_ir
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
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()


def test_ffi_naming_helpers_match_i1_conventions():
    assert _ddl_single_index_name("user", "email") == "idx_user_email"
    assert _ddl_single_unique_name("user", "email") == "uq_user_email"
    assert _ddl_check_constraint_name("user", "role") == "ck_user_role"
    assert (
        _ddl_fk_name("account", "org_id", "organization")
        == "fk_account_org_id_organization"
    )
    assert _ddl_composite_index_name("user", ["a", "b"]) == "idx_user_a_b"
    assert _ddl_composite_unique_name("user", ["a", "b"]) == "uq_user_a_b"


def test_ffi_naming_helpers_guard_63_chars():
    for name in [
        _ddl_single_index_name("t" * 40, "c" * 40),
        _ddl_single_unique_name("t" * 40, "c" * 40),
        _ddl_check_constraint_name("t" * 40, "c" * 40),
        _ddl_fk_name("t" * 30, "c" * 30, "o" * 30),
        _ddl_composite_index_name("t" * 40, ["c" * 40]),
        _ddl_composite_unique_name("t" * 40, ["c" * 40]),
    ]:
        assert len(name) == 63, name


# 61 chars: long enough that idx_/uq_ + "member_" pushes past 63.
_LONG_COL = "extremely_long_column_name_used_to_verify_truncation_guard_x"


def _build_models():
    class Org(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        slug: Annotated[str, FerroField(unique=True)]
        members: Relation[list["Member"]] = BackRef()

    class Member(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]
        extremely_long_column_name_used_to_verify_truncation_guard_x: Annotated[
            str, FerroField(index=True, unique=True)
        ]
        org: Annotated[Org, ForeignKey(related_name="members", index=True)]

        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("email", "org_id"),
        )

    return Org, Member


def test_compiler_ir_names_are_the_ffi_names():
    """The compiler's index/unique/fk names — including >63-char ones the old
    unguarded Python helpers got wrong — byte-match the shared Rust builders."""
    _build_models()
    envelope = compile_registry_schema_ir()
    models = envelope["payload"]["models"]
    member = next(m for m in models if m["table_name"] == "member")

    index_names = {i["name"] for i in member["indexes"]}
    unique_names = {u["name"] for u in member["uniques"]}

    assert _ddl_single_index_name("member", _LONG_COL) in index_names
    assert _ddl_single_unique_name("member", _LONG_COL) in unique_names
    assert _ddl_single_index_name("member", "org_id") in index_names
    assert _ddl_single_unique_name("member", "email") in unique_names
    assert _ddl_composite_index_name("member", ["email", "org_id"]) in index_names
    for name in index_names | unique_names:
        assert len(name) <= 63, name

    fk_names = {fk.get("name") for fk in member["foreign_keys"]}
    assert _ddl_fk_name("member", "org_id", "org") in fk_names


def test_metadata_fk_constraints_are_named():
    """The Alembic bridge renders the IR FK name on a table-level constraint."""
    _build_models()
    metadata = get_metadata()
    member = metadata.tables["member"]

    fk_constraints = [
        c for c in member.constraints if isinstance(c, sa.ForeignKeyConstraint)
    ]
    assert len(fk_constraints) == 1
    assert fk_constraints[0].name == "fk_member_org_id_org"
    (fk_element,) = fk_constraints[0].elements
    assert fk_element.target_fullname == "org.id"
    assert fk_element.ondelete == "CASCADE"


def test_metadata_single_column_unique_is_named_unique_index():
    """Single-column uniques are standalone named `uq_` unique indexes — the
    same shape the Rust emitter renders — not column flags SA would
    materialize as a differently-shaped UniqueConstraint."""
    _build_models()
    metadata = get_metadata()
    member = metadata.tables["member"]

    index_names = {idx.name: idx for idx in member.indexes}
    assert "uq_member_email" in index_names
    assert index_names["uq_member_email"].unique is True
    # The column itself carries no unique flag (that shape reflects differently).
    assert member.c.email.unique is not True
    # No single-column UniqueConstraint object remains.
    single_uqs = [
        c
        for c in member.constraints
        if isinstance(c, sa.UniqueConstraint) and len(list(c.columns)) == 1
    ]
    assert single_uqs == []


def test_custom_table_name_round_trips_both_emitters():
    """FF-E E2 exit gate: __ferro_table__ flows into the IR and both emitters."""
    import json

    from ferro._core import _render_create_table_sql_for_test

    class BillingAccount(Model):
        __ferro_table__: ClassVar[str] = "billing_accounts"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]

    envelope = compile_registry_schema_ir()
    model = next(
        m for m in envelope["payload"]["models"] if m["table_name"] == "billing_accounts"
    )
    assert _ddl_single_unique_name("billing_accounts", "email") in {
        u["name"] for u in model["uniques"]
    }

    payload_json = json.dumps(envelope["payload"])
    for dialect in ("sqlite", "postgres"):
        create_sql, _post, _pre = _render_create_table_sql_for_test(
            "billing_accounts", payload_json, dialect
        )
        assert '"billing_accounts"' in create_sql, (dialect, create_sql)
        assert '"billingaccount"' not in create_sql

    metadata = get_metadata()
    assert "billing_accounts" in metadata.tables
    assert "billingaccount" not in metadata.tables


def test_metadata_single_column_index_is_named_index():
    """Single-column indexes are explicit named `idx_` sa.Index objects, not
    `Column(index=True)` flags (which would use SA's `ix_` naming)."""
    _build_models()
    metadata = get_metadata()
    member = metadata.tables["member"]

    index_names = {idx.name for idx in member.indexes}
    assert "idx_member_org_id" in index_names
    assert member.c.org_id.index is not True
    # Nothing in the table uses SQLAlchemy's default ix_ prefix.
    assert not any(str(n).startswith("ix_") for n in index_names)


def test_relation_to_custom_table_model_points_at_configured_table():
    """FF-E E2 exit gate: FK to_table follows the target's __ferro_table__."""

    class OrgTeam(Model):
        __ferro_table__: ClassVar[str] = "org_teams"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        players: Relation[list["TeamPlayer"]] = BackRef()

    class TeamPlayer(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        team: Annotated[OrgTeam, ForeignKey(related_name="players")]

    envelope = compile_registry_schema_ir()
    player = next(
        m for m in envelope["payload"]["models"] if m["table_name"] == "teamplayer"
    )
    (fk,) = player["foreign_keys"]
    assert fk["to_table"] == "org_teams"
    assert fk["name"] == _ddl_fk_name("teamplayer", "team_id", "org_teams")

    metadata = get_metadata()
    fk_constraints = [
        c
        for c in metadata.tables["teamplayer"].constraints
        if isinstance(c, sa.ForeignKeyConstraint)
    ]
    (fk_element,) = fk_constraints[0].elements
    assert fk_element.target_fullname == "org_teams.id"


def test_m2m_join_artifacts_follow_custom_table_names():
    """FF-E E2 exit gate: default join table/columns derive from table names."""
    from ferro import ManyToMany
    from ferro.relations import resolve_relationships
    from ferro.state import _JOIN_TABLE_REGISTRY

    class WikiPage(Model):
        __ferro_table__: ClassVar[str] = "wiki_pages"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        tags: Relation[list["WikiTag"]] = ManyToMany(related_name="pages")

    class WikiTag(Model):
        __ferro_table__: ClassVar[str] = "wiki_tags"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        pages: Relation[list[WikiPage]] = BackRef()

    resolve_relationships()

    assert "wiki_pages_tags" in _JOIN_TABLE_REGISTRY
    join_schema = _JOIN_TABLE_REGISTRY["wiki_pages_tags"]
    props = join_schema["properties"]
    assert set(props) == {"wiki_pages_id", "wiki_tags_id"}
    assert props["wiki_pages_id"]["foreign_key"]["to_table"] == "wiki_pages"
    assert props["wiki_tags_id"]["foreign_key"]["to_table"] == "wiki_tags"


def test_forward_declared_fk_to_custom_table_model_resolves_configured_table():
    """FF-E E2: a string/ForwardRef FK target's to_table follows the target's
    __ferro_table__ — both while the reference is still an unresolved string
    (registry lookup via resolve_model_reference) and after
    resolve_relationships binds it (the schema DDL consumes)."""
    from ferro.relations import resolve_relationships

    class FwdArticle(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        author: Annotated["FwdWriter", ForeignKey(related_name="articles")]

    class FwdWriter(Model):
        __ferro_table__: ClassVar[str] = "staff_writers"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        articles: Relation[list[FwdArticle]] = BackRef()

    def _article_fk():
        envelope = compile_registry_schema_ir()
        article = next(
            m for m in envelope["payload"]["models"] if m["table_name"] == "fwdarticle"
        )
        (fk,) = article["foreign_keys"]
        return fk

    # Pre-resolution: metadata.to is still the string/ForwardRef, but the
    # target IS registered — _target_table_name's string branch must resolve
    # it through resolve_model_reference, not lowercase the class name.
    raw_to = FwdArticle.ferro_relations["author"].to
    assert isinstance(raw_to, (str, ForwardRef))
    fk = _article_fk()
    assert fk["to_table"] == "staff_writers"
    assert fk["name"] == _ddl_fk_name("fwdarticle", "author_id", "staff_writers")

    # Post-resolution (what DDL consumes): same configured table.
    resolve_relationships()
    fk = _article_fk()
    assert fk["to_table"] == "staff_writers"
    assert fk["name"] == _ddl_fk_name("fwdarticle", "author_id", "staff_writers")


def test_target_table_name_unresolvable_string_falls_back_to_lowercase():
    """An unregistered forward-ref string keeps the provisional .lower()
    fallback; resolve_relationships' loud second pass (FF-E E4) re-registers
    with the real table before any DDL consumer runs."""
    from ferro.schema_metadata import _target_table_name

    assert _target_table_name("NotRegisteredAnywhere") == "notregisteredanywhere"
    assert (
        _target_table_name(ForwardRef("NotRegisteredAnywhere"))
        == "notregisteredanywhere"
    )
