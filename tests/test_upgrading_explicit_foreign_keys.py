"""The upgrading guide's explicit foreign-key section.

The section holds the conversion table, the call-site changes, the acceptance
check and one prompt for a coding agent. The removed-name refusals link to its
anchor and the conversion slices run its prompt as written, so the page text is
pinned here; its fences run in ``tests/test_docs_examples.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._docs_sql import captured

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
    [row] = [
        line for line in _table(_section()).splitlines() if "ManyToMany(" in line
    ]
    assert 'through="tag_notes"' in row


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
