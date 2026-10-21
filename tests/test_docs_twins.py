"""Each example's Annotated twin is the same program in the other declaration style.

AGENTS.md I-7 shows every field-declaring example in an Assignment tab and an
Annotated tab. For every ``docs/examples/<name>.py`` with a
``<name>_annotated.py``:

- the twin is the Assignment script with each ``Field(...)`` declaration
  rewritten into the Annotated idiom (``tests._docs_twins.annotated_twin``),
  and nothing else changed, so the tabs differ only in field syntax;
- both declare the same schema (the registered models' SchemaIR each time the
  script connects or creates tables) and print the same output.

After editing an Assignment script, run
``python -m tests._docs_twins docs/examples/<name>.py`` to rewrite its twin.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests._docs_sql import captured
from tests._docs_twins import annotated_twin, twin_pairs


def _twin(body: str) -> str:
    return annotated_twin(textwrap.dedent(body).lstrip("\n"))


def test_options_move_inside_annotated_and_the_default_stays_assigned() -> None:
    assert _twin(
        """
        from ferro import Field, Model


        class User(Model):
            id: int | None = Field(default=None, primary_key=True)
            email: str = Field(unique=True)
            nickname: str | None = Field(None, index=True)
            name: str
        """
    ) == textwrap.dedent(
        """\
        from typing import Annotated

        from ferro import Field, Model


        class User(Model):
            id: Annotated[int | None, Field(primary_key=True)] = None
            email: Annotated[str, Field(unique=True)]
            nickname: Annotated[str | None, Field(index=True)] = None
            name: str
        """
    )


def test_a_default_factory_stays_an_assigned_field() -> None:
    """Type checkers do not see a default inside Annotated, so the factory stays
    the assigned value; only the other options move."""
    assert _twin(
        """
        import uuid
        from datetime import UTC, datetime

        from ferro import Field, Model


        class Event(Model):
            id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
            at: datetime = Field(default_factory=lambda: datetime.now(UTC))
        """
    ) == textwrap.dedent(
        """\
        import uuid
        from datetime import UTC, datetime
        from typing import Annotated

        from ferro import Field, Model


        class Event(Model):
            id: Annotated[uuid.UUID, Field(primary_key=True)] = Field(
                default_factory=uuid.uuid4
            )
            at: datetime = Field(default_factory=lambda: datetime.now(UTC))
        """
    )


def test_existing_annotated_metadata_and_typing_imports_are_kept() -> None:
    assert _twin(
        """
        from typing import TYPE_CHECKING, Annotated

        from ferro import Field, ForeignKey, Model


        class Post(Model):
            author: Annotated["Author", ForeignKey(related_name="posts")]
            slug: Annotated[str, "slug"] = Field(unique=True)  # a URL part
        """
    ) == textwrap.dedent(
        """\
        from typing import TYPE_CHECKING, Annotated

        from ferro import Field, ForeignKey, Model


        class Post(Model):
            author: Annotated["Author", ForeignKey(related_name="posts")]
            slug: Annotated[str, "slug", Field(unique=True)]  # a URL part
        """
    )


def test_a_comment_quoting_a_declaration_quotes_it_in_the_same_idiom() -> None:
    assert _twin(
        """
        from typing import Annotated

        from ferro import Field, Model


        class Author(Model):
            # before: nickname: str | None = Field(default=None, index=True)
            handle: str | None = Field(default=None, renamed_from="nickname")
        """
    ).splitlines()[6:8] == [
        "    # before: nickname: Annotated[str | None, Field(index=True)] = None",
        '    handle: Annotated[str | None, Field(renamed_from="nickname")] = None',
    ]


def test_a_comment_inside_a_multi_line_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="line 5: a comment inside a multi-line"):
        _twin(
            """
            from ferro import Field, Model

            class User(Model):
                email: str = Field(
                    unique=True,  # one account per address
                )
            """
        )


@pytest.mark.parametrize(
    ("assignment", "annotated"), twin_pairs(), ids=lambda path: path.name
)
def test_a_twin_is_its_assignment_script_rewritten(
    assignment: Path, annotated: Path
) -> None:
    expected = annotated_twin(assignment.read_text(encoding="utf-8"))
    assert annotated.read_text(encoding="utf-8") == expected, (
        f"docs/examples/{annotated.name} is not docs/examples/{assignment.name} "
        f"in the Annotated idiom; run python -m tests._docs_twins "
        f"docs/examples/{assignment.name} to rewrite it"
    )


@pytest.mark.parametrize(
    ("assignment", "annotated"), twin_pairs(), ids=lambda path: path.name
)
def test_twins_declare_the_same_schema_and_print_the_same(
    assignment: Path, annotated: Path
) -> None:
    left, right = captured(assignment.name), captured(annotated.name)
    assert right.schemas == left.schemas, (
        f"{annotated.name} declares a different schema from {assignment.name}"
    )
    assert right.stdout == left.stdout, (
        f"{annotated.name} prints something other than {assignment.name}"
    )
