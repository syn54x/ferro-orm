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

    await connect(db_url)
    async with engines.session():
        columns = await fetch_all(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name IN ('team', 'document') ORDER BY 1, 2"
        )
    assert [(row["table_name"], row["column_name"]) for row in columns] == [
        ("team", "id")
    ]
