"""A foreign key references the target's primary-key column, not a literal `id`.

```python
class Account(Model):
    code: str = Field(primary_key=True)
    entries: Relation[list["Entry"]] = BackRef()

class Entry(Model):
    id: int | None = Field(default=None, primary_key=True)
    account: Annotated[Account, ForeignKey(related_name="entries")]
```

```sql
CONSTRAINT "fk_entry_account_id_account" FOREIGN KEY ("account_id") REFERENCES "account" ("code")
```
"""

# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
import asyncio
from typing import Annotated

import pytest

import ferro
from ferro import BackRef, Field, ForeignKey, Model, Relation
from ferro.ir import compile_registry_schema_ir
from tests._pass_harness import auto_migrate, schema_sql
from tests.test_alembic_translator import _drift
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
)

pytestmark = pytest.mark.usefixtures("clean_registry")


def declare_code_keyed() -> tuple[type[Model], type[Model]]:
    class Account(Model):
        code: str = Field(primary_key=True)
        entries: Relation[list["Entry"]] = BackRef()

    class Entry(Model):
        id: int | None = Field(default=None, primary_key=True)
        account: Annotated[Account, ForeignKey(related_name="entries")]

    return Account, Entry


def model_fks(name: str) -> list[dict]:
    models = compile_registry_schema_ir()["payload"]["models"]
    return next(m for m in models if m["table_name"] == name.lower())["foreign_keys"]


def entry_fks() -> list[dict]:
    return model_fks("Entry")


def test_declared_ir_carries_the_target_primary_key_column():
    declare_code_keyed()
    assert entry_fks()[0]["to_column"] == "code"


def test_a_target_keyed_id_still_gets_id():
    class Owner(Model):
        id: int | None = Field(default=None, primary_key=True)
        pets: Relation[list["Pet"]] = BackRef()

    class Pet(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner: Annotated[Owner, ForeignKey(related_name="pets")]

    fk = model_fks("Pet")[0]
    assert fk["to_column"] == "id"


@pytest.mark.backend_matrix
def test_a_string_target_declared_later_is_recompiled_with_its_key(db_url):
    class Entry(Model):
        id: int | None = Field(default=None, primary_key=True)
        account: Annotated["Account", ForeignKey(related_name="entries")]

    class Account(Model):
        code: str = Field(primary_key=True)
        entries: Relation[list["Entry"]] = BackRef()

    report = asyncio.run(auto_migrate(db_url))
    assert any('REFERENCES "account" ("code")' in s for s in schema_sql(report))
    assert entry_fks()[0]["to_column"] == "code"


@pytest.mark.backend_matrix
def test_create_and_traverse_on_a_code_keyed_target(db_url):
    Account, Entry = declare_code_keyed()

    async def go():
        report = await auto_migrate(db_url)
        assert any('REFERENCES "account" ("code")' in s for s in schema_sql(report))
        async with ferro.engines.session():
            account = await Account.create(code="A1")
            entry = await Entry.create(account=account)
            assert entry.account_id == "A1"
            rows = await Entry.where(lambda entry: entry.account.code == "A1").all()
            assert [r.account_id for r in rows] == ["A1"]

    asyncio.run(go())


@pytest.mark.backend_matrix
def test_drift_is_clean_after_the_pass(db_url):
    declare_code_keyed()

    async def go():
        await auto_migrate(db_url)
        return await _drift(db_url)

    assert asyncio.run(go()) == []


@pytest.mark.usefixtures("isolated_imports")
def test_migrate_new_renders_the_same_constraint(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(
        project,
        pkg,
        """from typing import Annotated
from ferro import BackRef, Field, ForeignKey, Model, Relation


class Account(Model):
    code: str = Field(primary_key=True)
    entries: Relation[list["Entry"]] = BackRef()


class Entry(Model):
    id: int | None = Field(default=None, primary_key=True)
    account: Annotated[Account, ForeignKey(related_name="entries")]
""",
    )
    new("create_ledger")
    step = next(migrations(project).glob("0001_create_ledger/01_schema.up.*.sql"))
    assert 'REFERENCES "account" ("code")' in step.read_text()


def test_the_bridge_refuses_a_foreign_key_without_to_column():
    import sqlalchemy as sa

    from ferro.migrations.alembic import _build_sa_table_from_ir

    model_ir = {
        "table_name": "entry",
        "columns": [{"name": "account_id", "db_type": "text", "nullable": False}],
        "foreign_keys": [
            {"column": "account_id", "to_table": "account", "on_delete": None}
        ],
    }
    with pytest.raises(
        ValueError,
        match="foreign key entry.account_id carries no to_column in the schema IR",
    ):
        _build_sa_table_from_ir(sa.MetaData(), model_ir)


@pytest.mark.backend_matrix
def test_autogenerate_is_empty_against_the_auto_migrated_database(
    db_url, postgres_base_url, db_schema_name
):
    from tests._alembic_harness import autogenerate

    declare_code_keyed()
    asyncio.run(auto_migrate(db_url))
    upgrade, _ = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "op." not in upgrade, upgrade


def test_a_resolved_target_without_a_primary_key_is_refused():
    class Ledger(Model):
        name: str
        lines: Relation[list["Line"]] = BackRef()

    with pytest.raises(TypeError, match="primary_key=True field on Ledger"):

        class Line(Model):
            id: int | None = Field(default=None, primary_key=True)
            ledger: Annotated[Ledger, ForeignKey(related_name="lines")]
