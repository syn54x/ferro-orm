# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""A change that asks existing rows for values, generated end to end (#534;
ADR-0040, ADR-0042, ADR-0043, ADR-0035, ADR-0037).

```text
class Author(Model):
    ...
    slug: Annotated[str, FerroField(unique=True)]        # new, required
    __ferro_checks__ = (Check("slug_nonempty", lambda author: author.slug != ""),)

$ ferro migrate new author_slug
  0002_author_slug/
    01_expand          ADD COLUMN "slug" (nullable); its check NOT VALID on Postgres
    02_backfill_author.py
                       @chunked(…slug == None…)  author.slug = todo("the slug for an existing author")
    03_uq_author_slug  the unique index, built concurrently on Postgres
    04_add_constraint  "_ferro_notnull_author_slug" CHECK ("slug" IS NOT NULL) NOT VALID (Postgres)
    05_contract        VALIDATE …; SET NOT NULL; DROP the staging check (SQLite: rebuild)
```

Every case builds a real project under ``tmp_path`` targeting both dialects,
applies ``0001`` against the parametrized database, seeds 2,500 authors (so
the chunked backfill pages more than one batch of 1,000), generates the
change and checks the files, then that ``up`` refuses the unwritten
``todo``, applies once the value is written, the live schema has no drift
against the new snapshot, ``down`` reverts with no drift against the parent,
and ``up`` applies again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ferro.migrations.backfill_scaffold import backfill, guard, prefill_for
from tests.test_migrate_down import (  # noqa: F401 - fixtures
    keys,
    migration_dir,
    plan_against,
    snapshot,
    tables_of,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    isolated_imports,
    listing,
    pkg,
    project,
    run,
    write_config,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    db,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

backend_matrix = pytest.mark.backend_matrix
postgres_only = pytest.mark.postgres_only

ROWS = 2500


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: a rewritten ``models.py`` of the same size inside
    the same second would otherwise import the stale one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


# -- models -------------------------------------------------------------------------

HEAD = (
    "import uuid\nfrom typing import ClassVar\nfrom uuid import UUID\n\n"
    "from ferro import Check, Field\n" + AUTHOR
)
SLUG = "    slug: Annotated[str, FerroField(unique=True)]\n"
OPTIONAL_SLUG = "    slug: str | None = None\n"
REQUIRED_SLUG = "    slug: str\n"
NONEMPTY = (
    "    __ferro_checks__: ClassVar[tuple[Check, ...]] = (\n"
    '        Check("slug_nonempty", lambda author: author.slug != ""),\n'
    "    )\n"
)
TEAM = """
class Team(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
"""
MEMBERS = '    members: Relation[list["Author"]] = BackRef()\n'
REQUIRED_TEAM = '    team: Annotated[Team, ForeignKey(related_name="members", on_delete="CASCADE")]\n'

SLUG_TODO = 'todo("the slug for an existing author")'


# -- helpers ------------------------------------------------------------------------


def seed_authors(db, rows: int = ROWS) -> None:
    """``rows`` authors, written the way the table stands at ``0001``."""
    if db.backend == "sqlite":
        db.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
            f"WHERE i < {rows}) INSERT INTO author (name, status) "
            "SELECT 'author-' || i, 'draft' FROM n"
        )
    else:
        db.execute(
            "INSERT INTO author (name, status) SELECT 'author-' || i, 'draft'::status "
            f"FROM generate_series(1, {rows}) AS i"
        )


def start(project: Path, pkg: str, db, body: str, rows: int = ROWS) -> None:
    """``0001`` creates ``body``'s models for both dialects, is applied, and
    ``author`` holds ``rows`` rows."""
    write_config(project, pkg)
    write_models(project, pkg, body)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0
    if rows:
        seed_authors(db, rows)


def generate(project: Path, pkg: str, body: str, name: str, *flags: str) -> Path:
    """Generate ``0002`` for ``body``; returns its directory."""
    write_models(project, pkg, body)
    new(name, *flags)
    return migration_dir(project, 2)


def text(migration: Path, file: str) -> str:
    return (migration / file).read_text()


def write_value(migration: Path, step: str, todo: str, value: str) -> None:
    """Write the backfill: replace its ``todo(...)`` with ``value``."""
    path = migration / step
    source = path.read_text()
    assert todo in source, source
    path.write_text(source.replace(todo, value))


def ddl_files(*steps: str) -> list[str]:
    return [
        f"{step}.{direction}.{dialect}.sql"
        for step in steps
        for direction in ("up", "down")
        for dialect in ("postgres", "sqlite")
    ]


def clean(db, project: Path, number: int, other: int) -> bool:
    """No drift between the live schema and migration ``number``'s snapshot."""
    return plan_against(db, snapshot(project, number), snapshot(project, other)) == []


def round_trip(project: Path, db, steps: int) -> None:
    """``up`` applies ``0002``'s ``steps`` steps with no drift; ``down``
    reverts them with no drift against the parent; ``up`` applies them
    again."""
    assert run("migrate", "up", "--url", db.url) == 0
    assert keys(db) == [(1, 1), *[(2, n) for n in range(1, steps + 1)]]
    assert clean(db, project, 2, 1)
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1)]
    assert clean(db, project, 1, 2)
    assert run("migrate", "up", "--url", db.url) == 0
    assert clean(db, project, 2, 1)


def nulls(db, table: str, column: str) -> int:
    return db.rows(f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NULL')[0][0]


# -- A3: a required column over a populated table ------------------------------------


@backend_matrix
def test_a3_the_five_steps_refuse_the_todo_then_apply_and_round_trip(
    project, pkg, db, capsys
):
    start(project, pkg, db, HEAD)

    migration = generate(project, pkg, HEAD + SLUG + NONEMPTY, "author_slug")

    assert listing(migration) == sorted(
        ddl_files("01_expand", "03_uq_author_slug", "04_add_constraint", "05_contract")
        + ["02_backfill_author.py", "ir.json"]
    )
    assert text(migration, "01_expand.up.postgres.sql") == (
        'ALTER TABLE "author" ADD COLUMN "slug" varchar;\n\n'
        'ALTER TABLE "author" ADD CONSTRAINT "ck_author_slug_nonempty" '
        "CHECK (\"slug\" <> '') NOT VALID;\n"
    )
    assert text(migration, "01_expand.down.postgres.sql") == (
        'ALTER TABLE "author" DROP CONSTRAINT "ck_author_slug_nonempty";\n\n'
        'ALTER TABLE "author" DROP COLUMN "slug";\n'
    )
    backfill_text = text(migration, "02_backfill_author.py")
    assert (
        "@chunked(\n"
        "    lambda models: models.Author.where(lambda author: author.slug == None)\n"
        "    .order_by(lambda author: author.id),\n"
        "    batch_size=1000,\n"
        ")\n"
        "async def up(ctx, batch):\n"
        "    for author in batch:\n"
        f"        author.slug = {SLUG_TODO}\n"
        "        await author.save()\n"
    ) in backfill_text, backfill_text
    assert (
        '@nothing_to_reverse("01_expand.down.sql drops the column")\ndef down(ctx): ...\n'
    ) in backfill_text
    assert "--no-backfill author.slug" in backfill_text
    assert text(migration, "03_uq_author_slug.up.postgres.sql").startswith(
        "-- ferro: no-transaction\n"
    )
    assert text(migration, "04_add_constraint.up.postgres.sql") == (
        'ALTER TABLE "author" ADD CONSTRAINT "_ferro_notnull_author_slug" '
        'CHECK ("slug" IS NOT NULL) NOT VALID;\n'
    )
    assert text(migration, "04_add_constraint.up.sqlite.sql") == (
        "-- ferro: not-applicable\n"
    )
    assert text(migration, "05_contract.up.postgres.sql") == (
        "-- ferro: data-dependent\n\n"
        'ALTER TABLE "author" VALIDATE CONSTRAINT "_ferro_notnull_author_slug";\n\n'
        'ALTER TABLE "author" VALIDATE CONSTRAINT "ck_author_slug_nonempty";\n\n'
        'ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL;\n\n'
        'ALTER TABLE "author" DROP CONSTRAINT "_ferro_notnull_author_slug";\n'
    )
    assert text(migration, "05_contract.down.postgres.sql") == (
        'ALTER TABLE "author" ADD CONSTRAINT "_ferro_notnull_author_slug" '
        'CHECK ("slug" IS NOT NULL) NOT VALID;\n\n'
        'ALTER TABLE "author" ALTER COLUMN "slug" DROP NOT NULL;\n\n'
        'ALTER TABLE "author" DROP CONSTRAINT "ck_author_slug_nonempty";\n\n'
        'ALTER TABLE "author" ADD CONSTRAINT "ck_author_slug_nonempty" '
        "CHECK (\"slug\" <> '') NOT VALID;\n"
    )
    assert text(migration, "05_contract.up.sqlite.sql").startswith(
        "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n\n"
        'CREATE TABLE IF NOT EXISTS "_ferro_new_author"'
    )
    capsys.readouterr()

    # The unwritten value refuses the migration before anything runs.
    assert run("migrate", "up", "--url", db.url) == 1
    err = capsys.readouterr().err
    assert (
        "02_backfill_author.py:" in err
        and ("not written yet: the slug for an existing author") in err
    ), err
    assert keys(db) == [(1, 1)]

    write_value(migration, "02_backfill_author.py", SLUG_TODO, 'f"author-{author.id}"')
    round_trip(project, db, steps=5)
    assert nulls(db, "author", "slug") == 0
    assert db.rows("SELECT count(*) FROM author")[0][0] == ROWS


@pytest.mark.sqlite_only
def test_a_sqlite_only_project_has_no_add_constraint_and_contracts_by_rebuild(
    project, pkg, db
):
    write_config(project, pkg, '["sqlite"]')
    write_models(project, pkg, HEAD)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0
    seed_authors(db)

    migration = generate(project, pkg, HEAD + REQUIRED_SLUG, "author_slug")

    assert listing(migration) == [
        "01_expand.down.sqlite.sql",
        "01_expand.up.sqlite.sql",
        "02_backfill_author.py",
        "03_contract.down.sqlite.sql",
        "03_contract.up.sqlite.sql",
        "ir.json",
    ]
    write_value(migration, "02_backfill_author.py", SLUG_TODO, '"x"')
    round_trip(project, db, steps=3)


# -- A2b: a default_factory column ---------------------------------------------------


@backend_matrix
def test_a2b_a_standard_library_factory_is_prefilled_and_applies(project, pkg, db):
    start(project, pkg, db, HEAD)

    migration = generate(
        project,
        pkg,
        HEAD + "    token: UUID = Field(default_factory=uuid.uuid4)\n",
        "author_token",
    )

    source = text(migration, "02_backfill_author.py")
    assert "import uuid\n" in source
    assert "        author.token = uuid.uuid4()\n" in source, source
    assert "todo(" not in source
    # Nothing to write: up applies at once, every row gets its own value.
    assert run("migrate", "up", "--url", db.url) == 0
    assert nulls(db, "author", "token") == 0
    assert db.rows("SELECT count(DISTINCT token) FROM author")[0][0] == ROWS
    assert clean(db, project, 2, 1)


def test_a2b_any_other_factory_is_a_todo_naming_it(project, pkg):
    write_config(project, pkg)
    write_models(project, pkg, HEAD)
    new("create")
    factory = "\n\ndef make_token() -> str:\n    return 'x'\n\n"

    migration = generate(
        project,
        pkg,
        HEAD.replace(AUTHOR, factory + AUTHOR)
        + "    token: str = Field(default_factory=make_token)\n",
        "author_token",
    )

    assert (
        f'        author.token = todo("call {pkg}.models.make_token for an existing '
        'author")\n'
    ) in text(migration, "02_backfill_author.py")


def test_the_factory_is_recorded_in_the_snapshot_only_where_no_default_stands_in(
    project, pkg
):
    write_config(project, pkg)
    write_models(
        project,
        pkg,
        HEAD
        + "    token: UUID = Field(default_factory=uuid.uuid4)\n"
        + "    seen: datetime.datetime = Field(default_factory=datetime.datetime.now)\n",
    )
    (project / pkg / "models.py").write_text(
        "import datetime\n" + (project / pkg / "models.py").read_text()
    )
    new("create")
    columns = {
        col["name"]: col
        for col in snapshot(project, 1)["payload"]["models"][0]["columns"]
    }
    assert columns["token"]["default_factory"] == "uuid.uuid4"
    assert columns["seen"]["default_factory"] == "datetime.datetime.now"
    assert "default_factory" not in columns["name"]


# -- A7a, C1: the same shape -----------------------------------------------------------


@backend_matrix
def test_a7a_making_a_column_required_backfills_then_contracts(project, pkg, db):
    start(project, pkg, db, HEAD + OPTIONAL_SLUG)

    migration = generate(project, pkg, HEAD + REQUIRED_SLUG, "author_slug_required")

    assert listing(migration) == sorted(
        ddl_files("02_add_constraint", "03_contract")
        + ["01_backfill_author.py", "ir.json"]
    )
    source = text(migration, "01_backfill_author.py")
    assert (
        '@nothing_to_reverse("slug was nullable before this migration, so the values '
        'written stay")'
    ) in source
    write_value(migration, "01_backfill_author.py", SLUG_TODO, '"x"')
    round_trip(project, db, steps=3)


@backend_matrix
def test_c1_a_required_foreign_key_is_not_valid_in_the_expand_and_validated_in_the_contract(
    project, pkg, db
):
    start(project, pkg, db, TEAM + HEAD)
    db.execute("INSERT INTO team (label) VALUES ('core')")

    migration = generate(
        project, pkg, TEAM + MEMBERS + HEAD + REQUIRED_TEAM, "author_team"
    )

    assert text(migration, "01_expand.up.postgres.sql") == (
        'ALTER TABLE "author" ADD COLUMN "team_id" integer;\n\n'
        'ALTER TABLE "author" ADD CONSTRAINT "fk_author_team_id_team" FOREIGN KEY '
        '("team_id") REFERENCES "team" ("id") ON DELETE CASCADE NOT VALID;\n'
    )
    contract = text(migration, "04_contract.up.postgres.sql")
    assert contract.index('"_ferro_notnull_author_team_id"') < contract.index(
        'VALIDATE CONSTRAINT "fk_author_team_id_team"'
    )
    todo = 'todo("the team_id for an existing author")'
    write_value(migration, "02_backfill_author.py", todo, "1")
    round_trip(project, db, steps=4)


# -- several demands ------------------------------------------------------------------


def test_two_columns_share_a_backfill_and_two_models_get_one_each_parent_first(
    project, pkg
):
    # The child (author) sorts before its parent (team) by name.
    write_config(project, pkg)
    optional_team = (
        '    team: Annotated[Team | None, ForeignKey(related_name="members", '
        'on_delete="SET NULL")] = None\n'
    )
    write_models(project, pkg, TEAM + MEMBERS + HEAD + optional_team)
    new("create")

    migration = generate(
        project,
        pkg,
        TEAM
        + "    code: str\n"
        + MEMBERS
        + HEAD
        + optional_team
        + REQUIRED_SLUG
        + "    bio: str\n",
        "required_everywhere",
    )

    steps = [name for name in listing(migration) if name.endswith(".py")]
    assert steps == ["02_backfill_team.py", "03_backfill_author.py"], listing(migration)
    source = text(migration, "03_backfill_author.py")
    assert (
        "lambda models: models.Author.where(lambda author: (author.bio == None) | "
        "(author.slug == None))"
    ) in source
    assert "        if author.slug is None:\n" in source
    assert '"01_expand.down.sql drops the columns"' in source


def test_a_model_without_a_primary_key_is_backfilled_atomically():
    source = backfill(
        "Tag",
        ["slug"],
        driver="atomic",
        prefill={},
        template_dir=None,
        reverse="01_expand.down.sql drops the column",
    )
    assert "@atomic\nasync def up(ctx):\n" in source
    assert (
        "    await ctx.models.Tag.where(lambda tag: tag.slug == None).update(\n"
        '        slug=todo("the slug for an existing tag"),\n'
        "    )\n"
    ) in source
    compile(source, "02_backfill_tag.py", "exec")


def test_prefill_is_the_call_of_a_standard_library_factory_only():
    assert prefill_for("uuid.uuid4") == "uuid.uuid4()"
    assert prefill_for("datetime.datetime.now") == "datetime.datetime.now()"
    assert prefill_for("myapp.models.make_token") is None
    assert prefill_for("myapp.models.<lambda>") is None


def test_a_project_template_overrides_the_scaffolds(tmp_path):
    (tmp_path / "backfill.py").write_text("# {model}: {columns} over {query}\n")
    (tmp_path / "guard.py").write_text("# guard {model} {columns}\n")
    assert backfill(
        "Author",
        ["slug"],
        driver="chunked",
        prefill={},
        template_dir=tmp_path,
        key="id",
    ) == (
        "# Author: slug over models.Author.where(lambda author: author.slug == None)\n"
    )
    assert guard("Author", ["slug"], template_dir=tmp_path) == "# guard Author slug\n"


# -- --no-backfill: the guard step ------------------------------------------------------


@backend_matrix
def test_no_backfill_writes_a_guard_that_passes_empty_and_fails_populated_with_the_count(
    project, pkg, db, capsys
):
    start(project, pkg, db, HEAD, rows=0)

    migration = generate(
        project,
        pkg,
        HEAD + REQUIRED_SLUG,
        "author_slug",
        "--no-backfill",
        "author.slug",
    )

    assert "02_guard_author.py" in listing(migration)
    assert "02_backfill_author.py" not in listing(migration)
    # An empty table: nothing needs a value, the guard passes.
    round_trip(project, db, steps=4)

    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    seed_authors(db, 3)
    capsys.readouterr()
    assert run("migrate", "up", "--url", db.url) == 1
    err = capsys.readouterr().err
    assert "02_guard_author.py failed" in err, err
    assert "3 author rows still have NULL in slug" in err, err
    # The guard's failed record stands; nothing after it ran.
    assert keys(db) == [(1, 1), (2, 1), (2, 2)]


def test_no_backfill_naming_a_column_nothing_backfills_is_refused(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, HEAD)
    new("create")
    write_models(project, pkg, HEAD + REQUIRED_SLUG + "    bio: str\n")
    capsys.readouterr()

    assert run("migrate", "new", "x", "--no-backfill", "author.nickname") == 1
    assert (
        "--no-backfill author.nickname: no generated backfill fills that column; the "
        "columns this migration backfills: author.bio, author.slug"
    ) in capsys.readouterr().err

    assert run("migrate", "new", "x", "--no-backfill", "author.slug") == 1
    assert "author also needs a value for bio" in capsys.readouterr().err


# -- rows written behind the backfill's cursor ------------------------------------------

LATE_ROW = """\
        await author.save()
    if len(batch) < 1000:
        # A writer inserts a row behind the cursor (id 0) after the last batch.
        await ctx.execute(
            "INSERT INTO author (id, name, status) SELECT 0, 'late', status "
            "FROM author WHERE NOT EXISTS (SELECT 1 FROM author WHERE id = 0) LIMIT 1"
        )
"""


@backend_matrix
def test_a_late_null_row_fails_the_contract_with_the_count_and_the_recipe_that_works(
    project, pkg, db, capsys
):
    start(project, pkg, db, HEAD)
    migration = generate(project, pkg, HEAD + REQUIRED_SLUG, "author_slug")
    write_value(migration, "02_backfill_author.py", SLUG_TODO, '"x"')
    write_value(
        migration, "02_backfill_author.py", "        await author.save()\n", LATE_ROW
    )
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert f"0002_author_slug/04_contract.up.{db.backend}.sql failed" in err, err
    assert (
        '1 row still has NULL "slug" in "author"; run ferro migrate down --to 0002:01 '
        "then ferro migrate up to re-run the backfill"
    ) in err, err
    assert nulls(db, "author", "slug") == 1

    if db.backend == "postgres":
        # Mid-migration, the staging check is nobody's declared check: the
        # planner (the pass's reconciliation, the drift check) never drops it.
        live = db.rows(
            "SELECT conname FROM pg_constraint WHERE conname LIKE '_ferro_notnull_%'"
        )
        assert live == [("_ferro_notnull_author_slug",)]
        ops = plan_against(db, snapshot(project, 2), snapshot(project, 1))
        assert not [op for op in ops if "_ferro_notnull" in json.dumps(op)], ops

    # The recipe, as printed.
    assert run("migrate", "down", "--to", "0002:01", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1), (2, 1)]
    assert run("migrate", "up", "--url", db.url) == 0
    assert nulls(db, "author", "slug") == 0
    assert clean(db, project, 2, 1)
