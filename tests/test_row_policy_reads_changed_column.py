"""A row policy may read a column the same pass adds or drops on any table
(#995).

One pass adds ``team.name`` and creates ``document``, whose policy reads it;
or drops ``account.name`` together with the policy on ``document`` that read
it. Postgres refuses a ``CREATE POLICY`` over a column that does not exist
yet, and a ``DROP COLUMN`` under a policy that still reads it, so the one
planner puts every policy drop ahead of the first table or column change and
every policy create after the last. The pass then runs its table changes as
one transaction: a policy that fails leaves none of them.
"""

from enum import StrEnum
from typing import ClassVar

import pytest

from ferro import (
    Field,
    Model,
    OperationalError,
    RowPolicy,
    RowSecurity,
    connect,
    engines,
    reset_engine,
)
from ferro.raw import fetch_all
from tests._pass_harness import auto_migrate, schema_steps
from tests.test_row_security_create_pass import _rewind_registry, cleanup_registry  # noqa: F401

pytestmark = [
    pytest.mark.backend_matrix,
    pytest.mark.postgres_only,
    pytest.mark.asyncio,
]

ADD_NAME = ("team", 'ALTER TABLE "team" ADD COLUMN "name" varchar')
ENABLE = ("document", 'ALTER TABLE "document" ENABLE ROW LEVEL SECURITY')
FORCE = ("document", 'ALTER TABLE "document" FORCE ROW LEVEL SECURITY')
CREATE_POLICY = (
    "document",
    'CREATE POLICY "rls_document_members" ON "document" FOR ALL '
    'USING ("owner_id" IN (SELECT id FROM team WHERE name IS NOT NULL))',
)


def _members(table: str, column: str = "name") -> RowSecurity:
    return RowSecurity(
        RowPolicy(
            name="members",
            using=f'"owner_id" IN (SELECT id FROM {table} WHERE {column} IS NOT NULL)',
        )
    )


async def test_new_table_policy_reads_a_column_the_pass_adds(db_url):
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    await auto_migrate(db_url)
    _rewind_registry()

    class Team(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        name: str | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("team")

    report = await auto_migrate(db_url, updates=True)
    steps = schema_steps(report)
    assert steps[0][1].startswith('CREATE TABLE IF NOT EXISTS "document" (')
    assert steps[1:] == [ADD_NAME, ENABLE, FORCE, CREATE_POLICY]


async def test_existing_table_policy_reads_a_column_the_pass_adds(db_url):
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

    await auto_migrate(db_url)
    _rewind_registry()

    class Team(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        name: str | None = None

    class Document(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("team")

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [ADD_NAME, ENABLE, FORCE, CREATE_POLICY]


async def test_dropping_a_column_another_tables_dropped_policy_reads(db_url):
    class Account(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("account")

    await auto_migrate(db_url)
    _rewind_registry()

    class Account(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)

    class Document(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

    report = await auto_migrate(db_url, updates=True, destructive=True)
    assert schema_steps(report) == [
        ("document", 'DROP POLICY "rls_document_members" ON "document"'),
        ("account", 'ALTER TABLE "account" DROP COLUMN "name"'),
        ("document", 'ALTER TABLE "document" NO FORCE ROW LEVEL SECURITY'),
        ("document", 'ALTER TABLE "document" DISABLE ROW LEVEL SECURITY'),
    ]


async def test_a_policy_that_fails_rolls_back_every_table_change_of_the_pass(db_url):
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    await auto_migrate(db_url)
    _rewind_registry()

    class Team(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        name: str | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("team", "nope")

    with pytest.raises(OperationalError) as excinfo:
        await auto_migrate(db_url, updates=True)
    message = str(excinfo.value)
    assert "'document'" in message
    assert 'CREATE POLICY "rls_document_members"' in message
    assert schema_steps(excinfo.value.report) == []

    assert await _columns(db_url, "team", "document") == [("team", "id")]


async def _columns(db_url: str, *tables: str) -> list[tuple[str, str]]:
    """Every column of ``tables`` the database holds, as ``(table, column)``."""
    names = ", ".join(f"'{table}'" for table in tables)
    reset_engine()
    await connect(db_url)
    async with engines.session():
        columns = await fetch_all(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            f"AND table_name IN ({names}) ORDER BY 1, 2"
        )
    return [(row["table_name"], row["column_name"]) for row in columns]


async def test_a_failed_policy_leaves_no_plain_table_the_pass_would_create(db_url):
    """No new table declares row security, so the create pass would build
    ``team`` itself: it leaves it to the plan, whose one transaction then
    takes it back."""

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

    await auto_migrate(db_url)
    _rewind_registry()

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Document(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("team", "nope")

    with pytest.raises(OperationalError) as excinfo:
        await auto_migrate(db_url, updates=True)
    assert 'CREATE POLICY "rls_document_members"' in str(excinfo.value)
    assert schema_steps(excinfo.value.report) == []
    assert await _columns(db_url, "team", "document") == [
        ("document", "id"),
        ("document", "owner_id"),
    ]


async def test_a_failed_policy_rolls_back_a_rename_ahead_of_a_new_enum_type(db_url):
    """The plan's renames come ahead of its ``CREATE TYPE``: the type is
    created inside the one transaction, so the rename before it rolls back
    with every other change, and so does the type."""

    class Writer(Model):
        id: int | None = Field(default=None, primary_key=True)

    await auto_migrate(db_url)
    _rewind_registry()

    class Kind(StrEnum):
        STAFF = "staff"
        GUEST = "guest"

    class Author(Model):
        __ferro_renamed_from__ = "writer"

        id: int | None = Field(default=None, primary_key=True)
        kind: Kind | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("author", "nope")

    with pytest.raises(OperationalError) as excinfo:
        await auto_migrate(db_url, updates=True)
    assert 'CREATE POLICY "rls_document_members"' in str(excinfo.value)
    assert schema_steps(excinfo.value.report) == []
    assert await _columns(db_url, "writer", "author", "document") == [("writer", "id")]
    assert await _enum_types("kind") == []


async def _enum_types(*names: str) -> list[str]:
    """Which of ``names`` are enum types of the schema."""
    listed = ", ".join(f"'{name}'" for name in names)
    async with engines.session():
        rows = await fetch_all(
            "SELECT typname FROM pg_type WHERE typtype = 'e' "
            "AND typnamespace = current_schema()::regnamespace "
            f"AND typname IN ({listed}) ORDER BY 1"
        )
    return [row["typname"] for row in rows]


async def test_a_new_enum_type_takes_the_name_a_renamed_table_leaves(db_url):
    """A table's row type holds its name until the rename runs, so the
    ``CREATE TYPE`` stays after the renames, where the plan puts it."""

    class Writer(Model):
        id: int | None = Field(default=None, primary_key=True)

    await auto_migrate(db_url)
    _rewind_registry()

    class Writer(StrEnum):  # noqa: F811
        STAFF = "staff"
        GUEST = "guest"

    class Author(Model):
        __ferro_renamed_from__ = "writer"

        id: int | None = Field(default=None, primary_key=True)
        kind: Writer | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("author", "kind")

    report = await auto_migrate(db_url, updates=True)
    subjects = [subject for subject, _ in schema_steps(report)]
    assert subjects.index("author") < subjects.index("writer")
    assert ("author", "kind") in await _columns(db_url, "author")
    assert await _enum_types("writer") == ["writer"]


async def test_an_added_label_is_committed_ahead_of_the_one_transaction(db_url):
    """Postgres lets no statement use a label before it is committed, so a
    label addition alone runs first, on its own; it stays when the pass then
    fails."""

    class Kind(StrEnum):
        STAFF = "staff"

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        kind: Kind | None = None

    await auto_migrate(db_url)
    _rewind_registry()

    class Kind(StrEnum):  # noqa: F811
        STAFF = "staff"
        GUEST = "guest"

    class Team(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        kind: Kind | None = None
        name: str | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("team", "nope")

    with pytest.raises(OperationalError) as excinfo:
        await auto_migrate(db_url, updates=True)
    assert schema_steps(excinfo.value.report) == [
        ("kind", "ALTER TYPE \"kind\" ADD VALUE IF NOT EXISTS 'guest'")
    ]
    assert await _columns(db_url, "team", "document") == [
        ("team", "id"),
        ("team", "kind"),
    ]


async def _enum_labels(type_name: str) -> list[str]:
    """The labels of enum type ``type_name``, in the type's order; none when
    the schema has no such type."""
    async with engines.session():
        rows = await fetch_all(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typnamespace = current_schema()::regnamespace "
            f"AND t.typname = '{type_name}' ORDER BY e.enumsortorder"
        )
    return [row["enumlabel"] for row in rows]


async def test_a_label_added_beside_a_rename_hint_from_a_label_still_declared(
    db_url,
):
    """The models rename live label ``a`` to ``b`` and still declare ``a``.
    The planner refuses that hint, so no plan both renames a label and adds
    one under its old name: the pass, whose label addition commits ahead of
    its transaction, adds ``b`` and leaves the labels the models declare."""

    class Kind(StrEnum):
        A = "a"

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        kind: Kind | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

    await auto_migrate(db_url)
    _rewind_registry()

    class Kind(StrEnum):  # noqa: F811
        __ferro_renamed_labels__ = {"b": "a"}
        B = "b"
        A = "a"

    class Team(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        kind: Kind | None = None

    class Document(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("team", "kind")

    with pytest.warns(UserWarning, match="rename hint refused: enum Kind"):
        report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report)[0] == (
        "kind",
        "ALTER TYPE \"kind\" ADD VALUE IF NOT EXISTS 'b'",
    )
    assert await _enum_labels("kind") == ["a", "b"]


def _define_role_team_and_document(column: str) -> None:
    """``team.kind`` under the enum class renamed from ``Kind`` to ``Role``
    with the added label ``guest``, and ``document`` under a policy that
    reads ``team.<column>``."""

    class Role(StrEnum):
        STAFF = "staff"
        GUEST = "guest"

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        kind: Role | None = None
        name: str | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

        __ferro_rls__: ClassVar = _members("team", column)


async def _create_kind_team_and_document(db_url: str) -> None:
    class Kind(StrEnum):
        STAFF = "staff"

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        kind: Kind | None = None

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int

    await auto_migrate(db_url)
    _rewind_registry()


async def test_a_renamed_enum_class_gains_a_label_in_a_pass_that_adds_a_policy(db_url):
    await _create_kind_team_and_document(db_url)
    _define_role_team_and_document("name")

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report)[:2] == [
        ("role", 'ALTER TYPE "kind" RENAME TO "role"'),
        ("role", "ALTER TYPE \"role\" ADD VALUE IF NOT EXISTS 'guest'"),
    ]
    assert await _enum_labels("role") == ["staff", "guest"]
    assert await _enum_labels("kind") == []
    assert ("team", "name") in await _columns(db_url, "team")


async def test_a_failed_policy_leaves_the_added_label_and_its_types_rename(db_url):
    """What commits ahead of the one transaction stays: the label, and the
    rename of the type it was added to. No table change does."""
    await _create_kind_team_and_document(db_url)
    _define_role_team_and_document("nope")

    with pytest.raises(OperationalError) as excinfo:
        await auto_migrate(db_url, updates=True)
    assert schema_steps(excinfo.value.report) == [
        ("role", 'ALTER TYPE "kind" RENAME TO "role"'),
        ("role", "ALTER TYPE \"role\" ADD VALUE IF NOT EXISTS 'guest'"),
    ]
    assert await _columns(db_url, "team", "document") == [
        ("document", "id"),
        ("document", "owner_id"),
        ("team", "id"),
        ("team", "kind"),
    ]
    assert await _enum_labels("role") == ["staff", "guest"]
    assert await _enum_labels("kind") == []
