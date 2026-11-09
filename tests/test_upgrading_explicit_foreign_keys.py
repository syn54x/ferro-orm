# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The upgrading guide's explicit foreign-key section.

The section holds the conversion table, the call-site changes, the acceptance
check and one prompt for a coding agent. The removed-name refusals link to its
anchor and the conversion slices run its prompt as written, so the page text is
pinned here; its fences run in ``tests/test_docs_examples.py``.
"""

import asyncio
import importlib
import re
import shutil
from pathlib import Path
from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Field, Model, clear_registry, reset_engine
from tests._docs_sql import captured
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    pkg,
    project,
    write_config,
    write_models,
)
from tests.test_migrate_up import new

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE = REPO_ROOT / "docs" / "pages" / "upgrading.md"
HEADING = "## Upgrading to X.Y {#explicit-foreign-keys}"
REFERENCE = "Reference: https://ferro-orm.x54.sh/upgrading/#explicit-foreign-keys"

# One needle per old form the table converts.
OLD_FORMS = [
    "ForeignKey(",
    "nullable=False",
    "renamed_from=",
    "retired=True",
    "BackRef()",
    "ManyToMany(related_name=",
    "Field(back_ref=True)",
    "Field(many_to_many=True",
    "await ",
]
CALL_SITES = [
    "await note.account",
    "Note(body=",
    "await note.pin.first()",
    "ModelDoesNotExist",
    'extra="forbid"',
    "Field(uniqe=True)",
    "json_schema_extra",
    "db_type",
]


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _section() -> str:
    text = _page()
    start = text.index(HEADING)
    end = text.index("\n## ", start + len(HEADING))
    return text[start:end]


def _table(section: str) -> str:
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    assert rows, "the section has no conversion table"
    return "\n".join(rows)


def _prompt(section: str) -> str:
    start = section.index("??? example")
    fence = section.index("```text", start)
    return section[fence : section.index("```\n", fence + len("```text"))]


def test_the_section_is_the_first_upgrading_heading() -> None:
    headings = re.findall(r"^## Upgrading to .*$", _page(), flags=re.MULTILINE)
    assert headings[0] == HEADING
    assert headings.index(HEADING) < headings.index("## Upgrading to 0.26")


@pytest.mark.parametrize("old", OLD_FORMS)
def test_each_old_form_is_in_the_table_and_the_prompt(old: str) -> None:
    section = _section()
    assert old in _table(section)
    assert old in _prompt(section)


def test_the_table_has_a_row_per_old_form_with_its_new_form() -> None:
    rows = _table(_section()).splitlines()[2:]
    assert len(rows) == 10
    for row in rows:
        old, new = (cell.strip() for cell in row.strip("|").split(" | ", 1))
        assert old and new, row


def test_the_many_to_many_row_writes_the_derived_join_table_name() -> None:
    [row] = [line for line in _table(_section()).splitlines() if "ManyToMany(" in line]
    assert 'through="tag_notes"' in row


def _old_form_fence(section: str) -> str:
    start = section.index('```python {test=compile reason="shows the declarations')
    return section[start : section.index("```\n", start + 3)]


def test_the_old_optional_self_reference_is_one_old_ferro_accepted() -> None:
    # Old Ferro resolved a string target by class name, so "Note | None" was
    # never accepted; the optional key was nullable=True with a None default.
    section = _section()
    fence = _old_form_fence(section)
    assert '"Note | None", ForeignKey' not in fence
    assert 'nullable=True, on_delete="SET NULL"),\n    ] = None' in fence
    [row] = [line for line in _table(section).splitlines() if "reply_to: " in line]
    assert 'Annotated["Note", ForeignKey(' in row and "] = None" in row


def test_the_removed_keywords_get_their_own_refusal_not_a_suggestion() -> None:
    [paragraph] = [
        line
        for line in _section().splitlines()
        if line.startswith("The old relation keywords")
    ]
    assert 'no "Did you mean"' in paragraph
    assert "links this section" in paragraph
    assert "the same way;" not in paragraph


def test_the_section_holds_exactly_one_agent_prompt() -> None:
    section = _section()
    assert section.count("??? example") == 1
    prompt = _prompt(section)
    assert prompt.startswith(
        "```text\n    You are helping upgrade a Python codebase to Ferro ORM X.Y.\n"
        f"    {REFERENCE}\n"
    )


def test_the_prompt_carries_the_three_rules_and_the_lists() -> None:
    prompt = _prompt(_section())
    assert "declaration style" in prompt
    assert "| None" in prompt
    assert "lowercase singular" in prompt
    assert "through=" in prompt and "db_type" in prompt
    assert "ferro migrate check" in prompt


def test_the_tabs_pull_the_example_pair() -> None:
    section = _section()
    for script in ("upgrade_foreign_keys.py", "upgrade_foreign_keys_annotated.py"):
        assert f'--8<-- "docs/examples/{script}:models"' in section


@pytest.mark.parametrize("change", CALL_SITES)
def test_each_call_site_change_is_named(change: str) -> None:
    assert change in _section()


def test_the_acceptance_check_names_its_three_commands() -> None:
    section = _section()
    for command in ("ferro migrate check", "ferro migrate drift", "ferro.migrate()"):
        assert command in section


def test_every_python_fence_runs_or_says_why_not() -> None:
    fences = re.findall(r"```python(.*)$", _section(), flags=re.MULTILINE)
    assert fences
    for settings in fences:
        settings = settings.strip()
        assert settings in ("", "{test=run prelude=upgrade}") or settings.startswith(
            '{test=compile reason="'
        ), settings
    # A bare fence only embeds a snippet of the example pair.
    for body in re.findall(r"```python\n(.*?)```", _section(), flags=re.DOTALL):
        assert body.strip().startswith('--8<-- "docs/examples/upgrade_'), body


def test_the_example_pair_runs_and_declares_one_schema() -> None:
    assignment = captured("upgrade_foreign_keys.py")
    annotated = captured("upgrade_foreign_keys_annotated.py")
    assert assignment.schemas
    assert annotated.schemas == assignment.schemas


# -- a class whose options are written as Annotated[..., FerroField(...)] ----------

FERRO_FIELD_KEY = "account_id: Annotated[int, FerroField(foreign_key=Account)]"
FERRO_FIELD_RULE = (
    "In a class that writes its options as `Annotated[..., FerroField(...)]`"
)


def test_the_prompt_and_the_table_name_the_ferro_field_spelling() -> None:
    section = _section()
    prompt = " ".join(_prompt(section).split())
    assert FERRO_FIELD_RULE in prompt
    assert f"`{FERRO_FIELD_KEY}`" in prompt
    assert "`on_delete=` inside the same `FerroField`" in prompt
    [sentence] = [
        line for line in section.splitlines() if line.startswith(FERRO_FIELD_RULE)
    ]
    assert f"`{FERRO_FIELD_KEY}`" in sentence
    assert "`on_delete=` inside the same `FerroField`" in sentence
    assert section.index("| `await note.account` |") < section.index(sentence)
    assert section.index(sentence) < section.index("The `through=` name")


FERRO_FIELD_OPTIONAL = (
    "account_id: Annotated[int | None, "
    'FerroField(foreign_key=Account, on_delete="SET NULL")] = None'
)
ASSIGNMENT_OPTIONAL = (
    "account_id: int | None = "
    'Field(default=None, foreign_key=Account, on_delete="SET NULL")'
)
ANNOTATED_OPTIONAL = (
    "account_id: Annotated[int | None, "
    'Field(default=None, foreign_key=Account, on_delete="SET NULL")]'
)


def test_the_prompt_and_the_table_say_where_an_optional_keys_default_goes() -> None:
    section = _section()
    prompt = " ".join(_prompt(section).split())
    [sentence] = [
        line for line in section.splitlines() if line.startswith(FERRO_FIELD_RULE)
    ]
    for page_text in (prompt, sentence):
        assert f"`{FERRO_FIELD_OPTIONAL}`" in page_text
        assert "takes no `default=`" in page_text
    assert f"`{ASSIGNMENT_OPTIONAL}`" in prompt
    assert f"`{ANNOTATED_OPTIONAL}`" in prompt
    assert "Add `| None` (and `default=None`)" not in prompt


def _optional_key(style: str) -> object:
    """`Item.account_id` as the prompt writes an optional key in `style`."""
    clear_registry()

    class Account(Model):
        id: int | None = Field(default=None, primary_key=True)

    if style == "assignment":

        class Item(Model):
            id: int | None = Field(default=None, primary_key=True)
            account_id: int | None = Field(
                default=None, foreign_key=Account, on_delete="SET NULL"
            )

    elif style == "annotated":

        class Item(Model):
            id: int | None = Field(default=None, primary_key=True)
            account_id: Annotated[
                int | None,
                Field(default=None, foreign_key=Account, on_delete="SET NULL"),
            ]

    else:

        class Item(Model):
            id: int | None = Field(default=None, primary_key=True)
            account_id: Annotated[
                int | None, FerroField(foreign_key=Account, on_delete="SET NULL")
            ] = None

    return Item.__ferro_columns__["account_id"]


@pytest.mark.usefixtures("clean_registry")
def test_the_three_spellings_of_an_optional_key_declare_one_column() -> None:
    columns = [
        _optional_key(style) for style in ("assignment", "annotated", "ferro_field")
    ]
    assert columns[0] == columns[1] == columns[2]
    assert columns[0].nullable
    with pytest.raises(TypeError):
        FerroField(default=None)  # type: ignore[call-arg]


_FERRO_FIELD_IMPORTS = "from ferro import Field, Forward, Reverse\n"

# A class that declares its options with FerroField, converted by the rule
# above, one per `on_delete`. Before the old form was removed,
#     items: Relation[list["Item"]] = BackRef()
#     account: Annotated[Account, ForeignKey(related_name="items")]
# (with `Account | None`, `on_delete="SET NULL"` and `= None` for the second)
# sent SQLite `_OLD_FORM_DDL` and recorded the migration in
# `tests/fixtures/ferro_field_pre_conversion/<on_delete>/`.
_FERRO_FIELD_MODELS = {
    "CASCADE": """
class Account(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: Annotated[str, FerroField(unique=True)]
    items: Relation[list["Item"]] = Reverse("account_id")


class Item(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: Annotated[str, FerroField(index=True)]
    account_id: Annotated[int, FerroField(foreign_key=Account)]
    account: Relation[Account] = Forward("account_id")
""",
    "SET NULL": """
class Account(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: Annotated[str, FerroField(unique=True)]
    items: Relation[list["Item"]] = Reverse("account_id")


class Item(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: Annotated[str, FerroField(index=True)]
    account_id: Annotated[
        int | None, FerroField(foreign_key=Account, on_delete="SET NULL")
    ] = None
    account: Relation[Account | None] = Forward("account_id")
""",
}

_ACCOUNT_DDL = [
    'CREATE TABLE IF NOT EXISTS "account" ( "id" integer NOT NULL PRIMARY KEY '
    'AUTOINCREMENT, "name" varchar NOT NULL )',
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_account_name" ON "account" ("name")',
]
_OLD_FORM_DDL = {
    on_delete: [
        *_ACCOUNT_DDL,
        f'CREATE TABLE IF NOT EXISTS "item" ( "account_id" integer{not_null}, "id" '
        'integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar NOT NULL, '
        'CONSTRAINT "fk_item_account_id_account" FOREIGN KEY ("account_id") '
        f'REFERENCES "account" ("id") ON DELETE {on_delete} )',
        'CREATE INDEX IF NOT EXISTS "idx_item_title" ON "item" ("title")',
    ]
    for on_delete, not_null in (("CASCADE", " NOT NULL"), ("SET NULL", ""))
}
_OLD_FORM_MIGRATIONS = REPO_ROOT / "tests" / "fixtures" / "ferro_field_pre_conversion"


@pytest.fixture
def importable(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let `_ddl` import the models module in-process; the CLI adds the path itself."""
    monkeypatch.syspath_prepend(str(project))


def _ddl(project: Path, pkg: str, body: str) -> list[str]:
    """What `create_tables()` sends SQLite for `body`, written as a models module."""
    write_models(project, pkg, body)
    importlib.import_module(f"{pkg}.models")

    async def run() -> list[str]:
        await ferro.connect(f"sqlite:{project / 'ddl.db'}?mode=rwc")
        report = await ferro.create_tables()
        return [statement.sql for statement in report.statements]

    try:
        return asyncio.run(run())
    finally:
        (project / "ddl.db").unlink(missing_ok=True)
        reset_engine()


@pytest.mark.usefixtures("isolated_imports", "clean_registry", "importable")
@pytest.mark.parametrize("on_delete", ["CASCADE", "SET NULL"])
def test_a_ferro_field_class_converts_to_the_same_ddl(
    on_delete: str, project: Path, pkg: str
) -> None:
    after = _ddl(project, pkg, _FERRO_FIELD_IMPORTS + _FERRO_FIELD_MODELS[on_delete])

    assert after == _OLD_FORM_DDL[on_delete]
    [table] = [
        sql for sql in after if sql.startswith('CREATE TABLE IF NOT EXISTS "item"')
    ]
    assert f"ON DELETE {on_delete}" in table
    assert '"fk_item_account_id_account"' in table


@pytest.mark.usefixtures("isolated_imports", "clean_registry")
@pytest.mark.parametrize("on_delete", ["CASCADE", "SET NULL"])
def test_a_ferro_field_class_converts_and_migrate_new_plans_nothing(
    on_delete: str, project: Path, pkg: str, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(project, pkg)
    shutil.copytree(
        _OLD_FORM_MIGRATIONS / on_delete.replace(" ", "_"), project / "migrations"
    )
    recorded = sorted(path.name for path in (project / "migrations").iterdir())

    write_models(project, pkg, _FERRO_FIELD_IMPORTS + _FERRO_FIELD_MODELS[on_delete])
    new("convert")

    assert "no schema change: nothing written" in capsys.readouterr().out
    assert sorted(path.name for path in (project / "migrations").iterdir()) == recorded
