# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new`` for a declared rename (#528, ADR-0032).

```python
class Author(Model):
    __ferro_renamed_from__ = "writer"                 # table rename
    full_name: str = Field(renamed_from="name")       # column rename
```

```text
0002_rename_writer/
  01_schema.up.postgres.sql   ALTER TABLE "writer" RENAME TO "author";
                              ALTER TABLE "author" RENAME COLUMN "name" TO "full_name";
                              ALTER INDEX "idx_writer_name" RENAME TO "idx_author_full_name";
                              ALTER TABLE "book" RENAME CONSTRAINT "fk_book_writer_id_writer" TO "fk_book_writer_id_author";
  01_schema.up.sqlite.sql     ALTER TABLE "writer" RENAME TO "author"; … RENAME COLUMN …;
                              DROP INDEX "idx_writer_name"; CREATE INDEX "idx_author_full_name" …;
                              -- plus the rebuild of every table whose ck_/fk_ name moved
```

Every round trip builds a real project under ``tmp_path``, applies the parent
migration against the parametrized database (SQLite and Postgres), generates
the rename, applies it on a populated database, and checks there is no drift
against the new snapshot, that ``down`` reverts it with no drift against the
parent, and that every row survives both ways.
"""

import contextlib
import io
from typing import Annotated

import pytest

from ferro import BackRef, Field, ForeignKey, Model, Relation
from tests.test_generate_columns import (  # noqa: F401 - fixtures
    no_bytecode,
    refused,
    round_trip,
    schema_file,
    start,
)
from tests.test_migrate_down import (  # noqa: F401 - fixtures
    keys,
    migration_dir,
    plan_against,
    snapshot,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    isolated_imports,
    listing,
    pkg,
    project,
    run,
    statements,
    write_config,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

backend_matrix = pytest.mark.backend_matrix


def build_rename_hint_models() -> None:
    """Every rename hint, in both declaration styles (ADR-0032). Pins the
    ``schema_rename_hints_v2`` golden vector."""

    class Publisher(Model):
        id: int | None = Field(default=None, primary_key=True)
        books: Relation[list["Book"]] = BackRef()

    class Book(Model):
        __ferro_renamed_from__ = "volume"
        id: int | None = Field(default=None, primary_key=True)
        full_name: str = Field(index=True, renamed_from="name")
        subtitle: Annotated[str, Field(renamed_from="tagline")]
        house: Annotated[Publisher, ForeignKey("books", renamed_from="press")]

    _ = (Publisher, Book)


# -- the models ---------------------------------------------------------------------

IMPORTS = "from typing import ClassVar\n\nfrom ferro import Check, Field, ManyToMany\n"

GENRE = """
class Genre(StrEnum):
    NOVEL = "novel"
    POEM = "poem"
"""

# A5: `name` carries an index; `kind` an index and a db_check.
A5_BEFORE = IMPORTS + GENRE + """
class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = Field(index=True)
    kind: Annotated[Genre, FerroField(db_type="text", db_check=True, index=True)] = Genre.NOVEL
"""

A5_AFTER = IMPORTS + GENRE + """
class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    full_name: str = Field(index=True, renamed_from="name")
    genre: Annotated[
        Genre,
        FerroField(db_type="text", db_check=True, index=True, renamed_from="kind"),
    ] = Genre.NOVEL
"""


def b3(table: str, backend: str, hint: bool = True) -> str:
    """B3's models with the parent model's table named ``table``: a unique,
    a table check, a child foreign key, a many-to-many join table, and on
    Postgres a row policy."""
    model = table.capitalize()
    rls = (
        "    __ferro_rls__: ClassVar = RowSecurity(\n"
        '        RowPolicy(column="id", setting="app.writer_id"),\n'
        "    )\n"
        if backend == "postgres"
        else ""
    )
    renamed = '    __ferro_renamed_from__ = "writer"\n' if hint and table != "writer" else ""
    return IMPORTS + f"""
class Tag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    {table}s: Relation[list["{model}"]] = BackRef()


class {model}(Model):
{renamed}    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: Annotated[str, FerroField(unique=True)]
    books: Relation[list["Book"]] = BackRef()
    tags: Relation[list["Tag"]] = ManyToMany(related_name="{table}s")
    __ferro_checks__: ClassVar[tuple[Check, ...]] = (
        Check("named", lambda {table}: {table}.name != None),
    )
{rls}

class Book(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str
    writer: Annotated[{model}, ForeignKey("books")]
"""


def _new_capturing(name: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run("migrate", "new", name)
    return code, out.getvalue(), err.getvalue()


def rebuilt(up: list[str]) -> list[str]:
    """The tables a SQLite step file rebuilds, in order."""
    prefix = 'CREATE TABLE IF NOT EXISTS "_ferro_new_'
    return [sql[len(prefix) :].split('"', 1)[0] for sql in up if sql.startswith(prefix)]


# -- A5: rename a column ------------------------------------------------------------


@backend_matrix
def test_a_column_rename_renames_its_index_and_check_and_round_trips_with_rows(
    project, pkg, db
):
    start(project, pkg, db, A5_BEFORE)
    db.execute("INSERT INTO \"author\" (\"name\", \"kind\") VALUES ('Ann', 'poem')")
    write_models(project, pkg, A5_AFTER)
    new("author_full_name")

    up = statements(schema_file(project, 2, "up", db.backend))
    renames = [
        'ALTER TABLE "author" RENAME COLUMN "kind" TO "genre"',
        'ALTER TABLE "author" RENAME COLUMN "name" TO "full_name"',
    ]
    if db.backend == "postgres":
        assert up == renames + [
            'ALTER INDEX "idx_author_kind" RENAME TO "idx_author_genre"',
            'ALTER INDEX "idx_author_name" RENAME TO "idx_author_full_name"',
            'ALTER TABLE "author" RENAME CONSTRAINT "ck_author_kind" TO "ck_author_genre"',
        ]
    else:
        # SQLite: the column renames are native, an index is dropped and
        # built under its new name, and the check is renamed by the rebuild.
        assert up[:6] == renames + [
            'DROP INDEX IF EXISTS "idx_author_kind"',
            'CREATE INDEX IF NOT EXISTS "idx_author_genre" ON "author" ("genre")',
            'DROP INDEX IF EXISTS "idx_author_name"',
            'CREATE INDEX IF NOT EXISTS "idx_author_full_name" ON "author" ("full_name")',
        ]
        assert rebuilt(up) == ["author"]
        assert any('CONSTRAINT "ck_author_genre"' in sql for sql in up)
        assert "-- ferro: foreign-keys-off" in schema_file(
            project, 2, "up", db.backend
        ).read_text()
    assert "destructive" not in schema_file(project, 2, "up", db.backend).read_text()

    round_trip(project, db)
    assert db.rows('SELECT "full_name", "genre" FROM "author"') == [("Ann", "poem")]
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert db.rows('SELECT "name", "kind" FROM "author"') == [("Ann", "poem")]


@backend_matrix
def test_the_down_reverses_every_rename_in_the_reverse_shape(project, pkg, db):
    start(project, pkg, db, A5_BEFORE)
    write_models(project, pkg, A5_AFTER)
    new("author_full_name")

    # The down is planned from the migration's snapshot, so its renames
    # follow that snapshot's column and index order.
    down = statements(schema_file(project, 2, "down", db.backend))
    renames = [
        'ALTER TABLE "author" RENAME COLUMN "full_name" TO "name"',
        'ALTER TABLE "author" RENAME COLUMN "genre" TO "kind"',
    ]
    if db.backend == "postgres":
        assert down == renames + [
            'ALTER INDEX "idx_author_full_name" RENAME TO "idx_author_name"',
            'ALTER INDEX "idx_author_genre" RENAME TO "idx_author_kind"',
            'ALTER TABLE "author" RENAME CONSTRAINT "ck_author_genre" TO "ck_author_kind"',
        ]
    else:
        assert down[:2] == renames
        assert rebuilt(down) == ["author"]
    # A down is never destructive (ADR-0033).
    assert "destructive" not in schema_file(project, 2, "down", db.backend).read_text()


# -- B3: rename a table -------------------------------------------------------------


@backend_matrix
def test_a_table_rename_drags_every_owned_name_and_its_join_table_with_rows(
    project, pkg, db
):
    start(project, pkg, db, b3("writer", db.backend))
    db.execute("INSERT INTO \"writer\" (\"name\") VALUES ('Ann')")
    db.execute("INSERT INTO \"tag\" (\"label\") VALUES ('poetry')")
    db.execute('INSERT INTO "writer_tags" ("writer_id", "tag_id") VALUES (1, 1)')
    db.execute("INSERT INTO \"book\" (\"title\", \"writer_id\") VALUES ('Odes', 1)")
    write_models(project, pkg, b3("author", db.backend))
    new("rename_writer")

    up = statements(schema_file(project, 2, "up", db.backend))
    structural = [
        'ALTER TABLE "writer" RENAME TO "author"',
        'ALTER TABLE "writer_tags" RENAME TO "author_tags"',
        'ALTER TABLE "author_tags" RENAME COLUMN "writer_id" TO "author_id"',
    ]
    if db.backend == "postgres":
        assert up == structural + [
            'ALTER INDEX "uq_writer_name" RENAME TO "uq_author_name"',
            'ALTER INDEX "idx_writer_tags_tag_id_writer_id" RENAME TO '
            '"idx_author_tags_tag_id_author_id"',
            'ALTER INDEX "uq_writer_tags_writer_id_tag_id" RENAME TO '
            '"uq_author_tags_author_id_tag_id"',
            'ALTER TABLE "author" RENAME CONSTRAINT "ck_writer_named" TO "ck_author_named"',
            'ALTER TABLE "author_tags" RENAME CONSTRAINT "fk_writer_tags_tag_id_tag" TO '
            '"fk_author_tags_tag_id_tag"',
            'ALTER TABLE "author_tags" RENAME CONSTRAINT "fk_writer_tags_writer_id_writer" '
            'TO "fk_author_tags_author_id_author"',
            'ALTER TABLE "book" RENAME CONSTRAINT "fk_book_writer_id_writer" TO '
            '"fk_book_writer_id_author"',
            'ALTER POLICY "rls_writer_id" ON "author" RENAME TO "rls_author_id"',
        ]
    else:
        assert up[:3] == structural
        assert 'DROP INDEX IF EXISTS "uq_writer_name"' in up
        assert (
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_name" ON "author" ("name")' in up
        )
        # One rebuild per table whose constraint name moved, in one step.
        assert sorted(rebuilt(up)) == ["author", "author_tags", "book"]

    round_trip(project, db)
    assert db.rows('SELECT "name" FROM "author"') == [("Ann",)]
    assert db.rows('SELECT "author_id", "tag_id" FROM "author_tags"') == [(1, 1)]
    assert db.rows('SELECT "title", "writer_id" FROM "book"') == [("Odes", 1)]
    assert "writer" not in db.tables()
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert db.rows('SELECT "name" FROM "writer"') == [("Ann",)]
    assert db.rows('SELECT "writer_id", "tag_id" FROM "writer_tags"') == [(1, 1)]
    assert db.rows('SELECT "title", "writer_id" FROM "book"') == [("Odes", 1)]
    assert "author" not in db.tables()


# -- hint lifetime ------------------------------------------------------------------


@pytest.mark.parametrize("keep_hint", [True, False], ids=["kept", "deleted"])
def test_a_hint_after_its_migration_is_no_schema_change(project, pkg, capsys, keep_hint):
    write_config(project, pkg)
    write_models(project, pkg, A5_BEFORE)
    new("create")
    write_models(project, pkg, A5_AFTER)
    new("author_full_name")
    if not keep_hint:
        write_models(
            project,
            pkg,
            A5_AFTER.replace(', renamed_from="name"', "").replace(
                ', renamed_from="kind"', ""
            ),
        )
    written = listing(project / "migrations")
    capsys.readouterr()

    assert run("migrate", "new", "again") == 0
    assert "no schema change" in capsys.readouterr().out
    assert listing(project / "migrations") == written


# -- a rename and a type change in one edit ------------------------------------------


@backend_matrix
def test_a_rename_and_a_type_change_are_one_step_rename_first(project, pkg, db):
    start(project, pkg, db, A5_BEFORE)
    db.execute("INSERT INTO \"author\" (\"name\", \"kind\") VALUES ('Ann', 'poem')")
    write_models(
        project,
        pkg,
        A5_AFTER.replace(
            'full_name: str = Field(index=True, renamed_from="name")',
            'full_name: str = Field(index=True, renamed_from="name", db_type="varchar(80)")',
        ),
    )
    new("full_name_varchar")

    assert sorted(p.name for p in migration_dir(project, 2).iterdir()) == [
        f"01_schema.down.{db.backend}.sql",
        f"01_schema.up.{db.backend}.sql",
        "ir.json",
    ]
    up = statements(schema_file(project, 2, "up", db.backend))
    assert up[1] == 'ALTER TABLE "author" RENAME COLUMN "name" TO "full_name"'
    if db.backend == "postgres":
        assert up[-1].startswith('ALTER TABLE "author" ALTER COLUMN "full_name" TYPE')
    else:
        # One copy of the table carries the check rename and the type change.
        assert rebuilt(up) == ["author"]
    round_trip(project, db)
    assert db.rows('SELECT "full_name" FROM "author"') == [("Ann",)]


# -- refusals -----------------------------------------------------------------------


def test_a_hint_whose_old_name_is_still_declared_is_refused_naming_both(project, pkg):
    still = A5_AFTER.replace(
        '    full_name: str = Field(index=True, renamed_from="name")\n',
        '    full_name: str = Field(index=True, renamed_from="name")\n    name: str = ""\n',
    )
    err = refused(project, pkg, "sqlite", A5_BEFORE, still)
    assert 'author.full_name declares renamed_from="name"' in err
    assert 'author still declares "name"' in err


def test_two_hints_on_one_old_name_are_refused_naming_both(project, pkg):
    twice = A5_AFTER.replace(
        '    full_name: str = Field(index=True, renamed_from="name")\n',
        '    full_name: str = Field(index=True, renamed_from="name")\n'
        '    display_name: str | None = Field(default=None, renamed_from="name")\n',
    )
    err = refused(project, pkg, "postgres", A5_BEFORE, twice)
    assert "author.display_name" in err and "author.full_name" in err
    assert 'renamed_from="name"' in err


# -- no hint: a drop and an add -----------------------------------------------------


def test_a_drop_and_an_add_without_a_hint_is_destructive_and_names_renamed_from(
    project, pkg
):
    write_config(project, pkg)
    write_models(project, pkg, A5_BEFORE)
    new("create")
    write_models(
        project,
        pkg,
        A5_BEFORE.replace(
            "    name: str = Field(index=True)\n",
            "    full_name: str | None = Field(default=None, index=True)\n",
        ),
    )
    code, out, _ = _new_capturing("full_name")
    assert code == 0
    assert 'author: if "name" became "full_name", declare renamed_from="name"' in out
    for backend in ("postgres", "sqlite"):
        text = schema_file(project, 2, "up", backend).read_text()
        assert "-- ferro: destructive" in text
        up = statements(schema_file(project, 2, "up", backend))
        assert any(sql.startswith('ALTER TABLE "author" ADD COLUMN "full_name"') for sql in up)
        assert 'ALTER TABLE "author" DROP COLUMN "name"' in up
        assert not any("RENAME" in sql for sql in up)


# -- the declaration surface --------------------------------------------------------


def test_the_snapshot_records_every_hint(project, pkg):
    write_config(project, pkg, '["sqlite"]')
    write_models(project, pkg, A5_BEFORE)
    new("create")
    write_models(project, pkg, A5_AFTER)
    new("author_full_name")
    ir = snapshot(project, 2)
    assert ir["ir_version"] == 2
    (author,) = ir["payload"]["models"]
    assert {c["name"]: c.get("renamed_from") for c in author["columns"]} == {
        "full_name": "name",
        "genre": "kind",
        "id": None,
    }
