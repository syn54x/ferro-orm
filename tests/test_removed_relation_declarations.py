"""The old relation declarations are gone, each refused by name.

```python
from ferro import ForeignKey
# ImportError: ferro.ForeignKey is gone: foreign keys are columns, Field(foreign_key=...),
# and relations are Forward(), Reverse() and ManyToMany(). See https://ferro-orm.x54.sh/upgrading/#explicit-foreign-keys
```

Every refusal links the upgrading section; the form that replaces each one
declares the key column itself.
"""

import ast
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import ferro
from ferro import (
    Field,
    Forward,
    ManyToMany,
    Model,
    Relation,
    Reverse,
    clear_registry,
    reset_engine,
)
from ferro.registry import REGISTRY
from ferro.relations import resolve_relationships

URL = "https://ferro-orm.x54.sh/upgrading/#explicit-foreign-keys"


@pytest.fixture(autouse=True)
def cleanup():
    REGISTRY.reset_for_test()
    reset_engine()
    clear_registry()
    yield


# -- removed names ------------------------------------------------------------------


def test_importing_foreign_key_names_its_replacement():
    with pytest.raises(ImportError) as refused:
        from ferro import ForeignKey  # noqa: F401

    assert str(refused.value) == (
        "ferro.ForeignKey is gone: foreign keys are columns, "
        "Field(foreign_key=...), and relations are Forward(), Reverse() and "
        f"ManyToMany(). See {URL}"
    )


def test_importing_back_ref_names_reverse():
    with pytest.raises(ImportError) as refused:
        from ferro import BackRef  # noqa: F401

    assert str(refused.value) == (
        'ferro.BackRef is gone: the reverse side of a relation is Reverse("<column '
        f'on the other model>"). See {URL}'
    )


def test_reading_a_removed_name_off_the_module_is_the_same_refusal():
    with pytest.raises(ImportError, match=r"ferro\.ForeignKey is gone"):
        ferro.ForeignKey  # noqa: B018
    with pytest.raises(ImportError, match=r"ferro\.BackRef is gone"):
        ferro.BackRef  # noqa: B018

    assert "ForeignKey" not in ferro.__all__
    assert "BackRef" not in ferro.__all__


def test_any_other_missing_name_is_python_s_own_error():
    assert not hasattr(ferro, "NoSuchName")
    with pytest.raises(ImportError, match="Feld") as refused:
        from ferro import Feld  # noqa: F401

    assert URL not in str(refused.value)


@pytest.mark.parametrize(
    ("keyword", "value", "reason"),
    [
        (
            "back_ref",
            True,
            "a Field is a column; the reverse side of a relation is "
            'Reverse("<column>")',
        ),
        (
            "many_to_many",
            True,
            'a many-to-many is ManyToMany(through="<table>")',
        ),
        (
            "related_name",
            "players",
            "the two sides of a relation never name each other",
        ),
        (
            "through",
            "player_tournaments",
            'through= belongs on ManyToMany(through="<table>")',
        ),
        (
            "reverse_index",
            False,
            'reverse_index= belongs on ManyToMany(through="<table>", '
            "reverse_index=False)",
        ),
    ],
)
def test_field_refuses_each_old_relation_keyword(keyword, value, reason):
    with pytest.raises(TypeError) as refused:
        Field(**{keyword: value})

    assert str(refused.value) == f"Field({keyword}=...) is gone: {reason}. See {URL}"
    assert "Did you mean" not in str(refused.value)


@pytest.mark.parametrize(
    "options",
    [{"related_name": "players"}, {"related_name": "players", "through": "x"}],
    ids=["alone", "beside through"],
)
def test_many_to_many_refuses_related_name(options):
    with pytest.raises(TypeError) as refused:
        ManyToMany(**options)

    assert str(refused.value) == (
        "ManyToMany(related_name=...) is gone: the other model declares "
        f'Reverse("<this attribute>"). See {URL}'
    )


def test_a_misspelled_through_names_the_keyword():
    with pytest.raises(TypeError) as refused:
        ManyToMany(throgh="player_tournaments")

    assert str(refused.value) == (
        "ManyToMany() got an unknown keyword 'throgh'. Did you mean 'through'?"
    )


def test_a_many_to_many_without_through_is_refused_naming_the_table_to_write():
    class Tournament(Model):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(TypeError) as refused:

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            tournaments: Relation[list[Tournament]] = ManyToMany()

    assert str(refused.value).endswith(
        "Write: tournaments: Relation[list[Tournament]] = "
        'ManyToMany(through="player_tournaments")'
    )


# -- no text names a removed declaration ---------------------------------------------

_REMOVED = re.compile(r"\b(ForeignKey|BackRef)\b")


def _refusal_of_a_removed_name(path: Path, node: ast.AST) -> bool:
    """The refusals that name the removed declarations on purpose."""
    if path.name != "__init__.py" or path.parent.name != "ferro":
        return False
    if isinstance(node, ast.FunctionDef):
        return node.name == "__getattr__"
    return isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "_REMOVED_NAMES"
        for target in node.targets
    )


def test_no_message_or_docstring_names_a_removed_declaration():
    root = Path(ferro.__file__).parent
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skipped = {
            id(inner)
            for node in tree.body
            if _refusal_of_a_removed_name(path, node)
            for inner in ast.walk(node)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skipped
                and _REMOVED.search(node.value)
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno}")

    assert found == []


# -- what replaces them ---------------------------------------------------------------


def test_a_declared_key_column_keeps_its_type_and_names_its_relation():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        players: Relation[list["Player"]] = Reverse("team_id")

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key=Team, index=True)
        team: Relation[Team] = Forward("team_id")

    resolve_relationships()

    assert Player.model_fields["team_id"].annotation is int
    assert Player.__ferro_relation_specs__["team"].fk_column == "team_id"
    for model in (Team, Player):
        assert "BackRef" not in model.__ferro_relations__.values()


def test_a_key_column_to_a_later_model_keeps_its_declared_type():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key="Team")

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    resolve_relationships()

    assert Player.model_fields["team_id"].annotation is int


def test_a_required_key_column_refuses_none_through_validation():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key=Team)
        team: Relation[Team] = Forward("team_id")

    with pytest.raises(ValidationError) as refused:
        Player(team_id=None)

    assert [error["loc"] for error in refused.value.errors()] == [("team_id",)]
