"""A refusal's fix is a list of edits, printed once and applied here.

Every refusal of a declaration raises `DeclarationRefusal`, carrying its fix
as `Edit`s computed from the models. Its message is `render_fix` of those
edits, and the sweep below applies the same edits to the refused source, so
the text a user reads is the fix this file proves:

```python
class Player(Model):
    team_id: int = Field(foreign_key=Team, on_delete="SET NULL")
    team: Relation[Team] = Forward("team_id")
# Player.team_id: on_delete="SET NULL" needs a nullable column. Fix it by hand:
# (1) on Player, replace team_id with `team_id: int | None = Field(default=None, ...)`;
# (2) on Player, replace team with `team: Relation[Team | None] = Forward("team_id")`
```

A `Write:` line is printed only when every edit replaces the refused
declaration; any other fix is numbered steps. A refusal the reader can fix in
more than one way carries each way, and the sweep applies each one.
"""

import ast
import sys
from pathlib import Path

import pytest

import ferro
from ferro import Model
from ferro.base import ManyToManyRelation
from ferro.registry import REGISTRY
from ferro.relations.fixes import DeclarationRefusal, Edit, render_fix
from tests.test_postponed_annotations import _import, _wipe

# -- the renderer -------------------------------------------------------------------


def test_a_fix_replacing_only_the_refused_declaration_is_a_write_line():
    edits = [Edit("Player", "team_id", "replace", ("team_id: int = Field()",))]

    assert render_fix("Player", "team_id", "wrong", edits) == (
        "Player.team_id: wrong. Write: team_id: int = Field()"
    )


def test_a_fix_beyond_the_refused_declaration_is_numbered_steps():
    edits = [
        Edit("Player", "team_id", "replace", ("team_id: int | None = None",)),
        Edit("Team", "id", "add", ("id: str = Field(primary_key=True)",)),
        Edit("Player", "team", "remove"),
        Edit("PlayerLinks", "", "add", ("a: int", "b: int")),
    ]

    assert render_fix("Player", "team_id", "wrong", edits) == (
        "Player.team_id: wrong. Fix it by hand: (1) on Player, replace team_id "
        "with `team_id: int | None = None`; (2) on Team, add "
        "`id: str = Field(primary_key=True)`; (3) on Player, remove team; "
        "(4) add the model `class PlayerLinks(Model): a: int; b: int`"
    )


def test_several_local_ways_are_one_write_line_joined_by_or():
    ways = [
        [Edit("Team", "players", "replace", ('players: Relation[list["Player"]]',))],
        [Edit("Team", "players", "replace", ('players: Relation[list["Coach"]]',))],
    ]

    assert render_fix("Team", "players", "wrong", alternatives=ways) == (
        'Team.players: wrong. Write: players: Relation[list["Player"]] or '
        'players: Relation[list["Coach"]]'
    )


def test_several_ways_beyond_the_declaration_are_numbered_per_way():
    ways = [
        [Edit("Post", "tags", "replace", ("tags: A",)), Edit("Tag", "x", "remove")],
        [Edit("Post", "tags", "replace", ("tags: B",)), Edit("Tag", "y", "remove")],
    ]

    assert render_fix("Post", "tags", "wrong", alternatives=ways) == (
        "Post.tags: wrong. Fix it by hand: (1) on Post, replace tags with "
        "`tags: A`; (2) on Tag, remove x Or: (1) on Post, replace tags with "
        "`tags: B`; (2) on Tag, remove y"
    )


def test_a_refusal_with_no_edits_is_its_reason():
    assert render_fix("Post", "tags", "say what", []) == "Post.tags: say what"


# -- the sweep ----------------------------------------------------------------------
#
# Each case is a refused modelset: the lines of each model, the refused
# declaration (`owner`.`field`), what its fix must give (`kind`) and whether
# the fix is local (a `Write:` line), or `None` for a refusal that can name
# no fix. It runs over the target key's storage, both definition orders,
# both __future__ modes and, where a model names Team, Team written as a
# class (checked when the class is defined) or as a name (checked when
# relations resolve); most cases also run with the linked model another model
# or the owner, and with a relation reading the refused column or none. Each
# way to fix it is applied to the source with one helper, and the result
# must load, every model with a primary key.

_KEYS = {
    # storage: (primary-key line, key type)
    "int": ("id: int | None = Field(default=None, primary_key=True)", "int"),
    "str": ("code: str = Field(primary_key=True)", "str"),
    "varchar(8)": ('code: str = Field(primary_key=True, db_type="varchar(8)")', "str"),
}
_WRONG_TYPE = {"int": "str", "str": "int", "varchar(8)": "int"}
# A column whose storage is not its target key's, per target key storage.
_OTHER_STORAGE = {
    "int": 'Field(foreign_key="{t}", db_type="bigint")',
    "str": 'Field(foreign_key="{t}", db_type="varchar(8)")',
    "varchar(8)": 'Field(foreign_key="{t}")',
}
_LONG_FIELD = "linked_" + "x" * 56  # its derived join table passes 63 characters

# The models every case defines; a fix may add another of its own.
_MODELS = ("Team", "Tag", "Tournament", "Card", "Player")

# Each modelset is defined in both orders, so every model a fix names is
# sometimes defined before the line that names it and sometimes after.
_ORDERS = {"Player last": _MODELS, "Player first": tuple(reversed(_MODELS))}


def _case(
    kind,
    field,
    *,
    local,
    owner="Player",
    no_pk=(),
    after=(),
    bases=None,
    orders=tuple(_ORDERS),
    **models,
):
    """One refused modelset; ``models`` are the lines of each model, ``after``
    the module lines run once they are defined, ``bases`` a model's base
    model where it is not `Model`, and ``orders`` the definition orders the
    case runs in."""
    return {
        "kind": kind,
        "owner": owner,
        "field": field,
        "local": local,
        "no_pk": no_pk,
        "after": after,
        "bases": bases or {},
        "orders": orders,
        "models": models,
    }


def _merge(first: dict, second: dict) -> dict:
    merged = {name: list(lines) for name, lines in first.items()}
    for name, lines in second.items():
        merged.setdefault(name, []).extend(lines)
    return merged


def _key_cases(cases: dict, storage: str, t: str) -> None:
    """The refusals of a foreign-key column to ``t``, with and without a
    forward relation reading it."""
    _, key = _KEYS[storage]
    wrong = _WRONG_TYPE[storage]
    stored = ', db_type="varchar(8)"' if storage == "varchar(8)" else ""
    columns = {
        "key of the wrong type": (f'team_id: {wrong} = Field(foreign_key="{t}")', True),
        "key stored otherwise": (
            f"team_id: {key} = " + _OTHER_STORAGE[storage].format(t=t),
            True,
        ),
        "required SET NULL key": (
            f'team_id: {key} = Field(foreign_key="{t}", on_delete="SET NULL"{stored})',
            True,
        ),
        "key to a model with no primary key": (
            f'team_id: {key} = Field(foreign_key="{t}"{stored})',
            False,
        ),
    }
    if storage == "int":
        columns["generated key"] = (
            "team_id: int | None = "
            f'Field(foreign_key="{t}", generated=lambda player: player.id)',
            True,
        )
    for name, (line, local) in columns.items():
        no_pk = (t,) if "no primary key" in name else ()
        reader = f'team: Relation["{t}"] = Forward("team_id")'
        if name == "generated key":
            reader = f'team: Relation["{t} | None"] = Forward("team_id")'
        # A keyless Team with a column of its own is one the schema compile
        # of a model defined after it can read.
        team = {"Team": ['name: str = ""']} if no_pk == ("Team",) else {}
        cases[f"{name} to {t}"] = _case(
            "key",
            "team_id",
            local=local,
            no_pk=no_pk,
            **_merge(team, {"Player": [line]}),
        )
        # A reader makes SET NULL reach the relation: two declarations change.
        cases[f"{name} to {t}, read by a forward relation"] = _case(
            "key",
            "team_id",
            local=local and name != "required SET NULL key",
            no_pk=no_pk,
            **_merge(team, {"Player": [line, reader]}),
        )


def _cases(storage: str) -> dict[str, dict]:
    pk, key = _KEYS[storage]
    wrong = _WRONG_TYPE[storage]
    # A key column the case does not refuse, stored as its target key is.
    stored = ', db_type="varchar(8)"' if storage == "varchar(8)" else ""
    cases: dict[str, dict] = {}

    def fk(t, extra=""):
        return f'Field(foreign_key="{t}"{extra}{stored})'

    for t in ("Team", "Player"):
        _key_cases(cases, storage, t)
        cases[f"forward with no column to {t}"] = _case(
            "forward",
            "team",
            local=False,
            Player=[f'team: Relation["{t}"] = Forward("team_id")'],
        )
        cases[f"forward over a missing column, one other key to {t}"] = _case(
            "forward",
            "team",
            local=True,
            Player=[
                f"squad_id: {key} = {fk(t)}",
                f'team: Relation["{t}"] = Forward("team_id")',
            ],
        )
        cases[f"forward over a column with no foreign_key to {t}"] = _case(
            "forward",
            "team",
            local=False,
            Player=[f"team_id: {key}", f'team: Relation["{t}"] = Forward("team_id")'],
        )
        for spelling, relation in {
            "annotated as a list": f'Relation[list["{t}"]]',
            "annotated without Relation": f'"{t}"',
            "a bare Relation": "Relation",
            "nullable over a required column": f'Relation["{t} | None"]',
        }.items():
            cases[f"forward {spelling} to {t}"] = _case(
                "forward",
                "team",
                local=True,
                Player=[
                    f"team_id: {key} = {fk(t)}",
                    f'team: {relation} = Forward("team_id")',
                ],
            )
        cases[f"forward to another model than its column's, {t}"] = _case(
            "forward",
            "team",
            local=True,
            Player=[
                f"team_id: {key} = {fk(t)}",
                'team: Relation["Tag"] = Forward("team_id")',
            ],
        )
        cases[f"forward over a retired column to {t}"] = _case(
            "plain",
            "team",
            local=False,
            Player=[
                f"team_id: {key} | None = "
                f'Field(default=None, foreign_key="{t}", retired=True{stored})',
                f'team: Relation["{t} | None"] = Forward("team_id")',
            ],
        )
        reverse = 'players: Relation[list["Player"]] = Reverse("team_id")'
        cases[f"reverse on {t} over a column with no foreign_key"] = _case(
            "reverse",
            "players",
            local=False,
            owner=t,
            **_merge({"Player": [f"team_id: {key}"]}, {t: [reverse]}),
        )
        for spelling, relation in {
            "annotated as one row": 'Relation["Player"]',
            "a bare Relation": "Relation",
        }.items():
            cases[f"reverse on {t} {spelling}"] = _case(
                "reverse",
                "players",
                local=True,
                owner=t,
                **_merge(
                    {"Player": [f"team_id: {key} = {fk(t)}"]},
                    {t: [f'players: {relation} = Reverse("team_id")']},
                ),
            )
        cases[f"reverse on {t} over a retired column"] = _case(
            "plain",
            "players",
            local=False,
            owner=t,
            **_merge(
                {
                    "Player": [
                        f"team_id: {key} | None = "
                        f'Field(default=None, foreign_key="{t}", retired=True{stored})'
                    ]
                },
                {t: [reverse]},
            ),
        )
    player_pk = _field_of(pk)
    cases["reverse on Team naming Player's primary key"] = _case(
        "reverse",
        "players",
        local=False,
        owner="Team",
        Team=[f'players: Relation[list["Player"]] = Reverse("{player_pk}")'],
    )
    cases["bare reverse on Team two models point back to"] = _case(
        "reverse",
        "players",
        local=True,
        owner="Team",
        Player=[f"team_id: {key} = {fk('Team')}"],
        Card=[f"team_id: {key} = {fk('Team')}"],
        Team=['players: Relation = Reverse("team_id")'],
    )
    # Refusals that can name no fix, and say what to do in words.
    cases["bare forward over a column with no foreign_key"] = _case(
        "none",
        "team",
        local=None,
        Player=[f"team_id: {key}", 'team: Relation = Forward("team_id")'],
    )
    cases["bare forward over a missing column"] = _case(
        "none", "team", local=None, Player=['team: Relation = Forward("team_id")']
    )
    cases["bare reverse nothing points back to"] = _case(
        "none",
        "players",
        local=None,
        owner="Team",
        Team=['players: Relation = Reverse("team_id")'],
    )
    cases["bare many-to-many nothing names"] = _case(
        "none",
        "links",
        local=None,
        Player=['links: Relation = ManyToMany(through="player_links")'],
    )
    # None assigned to a relation over a required key column.
    assign_none = ("Player.model_construct().team = None",)
    cases["None assigned to a forward relation over a required key"] = _case(
        "plain",
        "team",
        local=False,
        after=assign_none,
        Player=[
            f"team_id: {key} = {fk('Team')}",
            'team: Relation["Team"] = Forward("team_id")',
        ],
    )
    cases["column with on_delete and no foreign_key"] = _case(
        "plain",
        "note",
        local=True,
        Player=['note: str | None = Field(default=None, on_delete="SET NULL")'],
    )
    # A Reverse on Player over its own columns adds its key to its own class.
    cases["reverse on Player with no column"] = _case(
        "reverse",
        "players",
        local=False,
        Player=['players: Relation[list["Player"]] = Reverse("team_id")'],
    )
    cases["reverse on Player over a column pointing elsewhere"] = _case(
        "reverse",
        "players",
        local=False,
        Player=[
            f'mentor_id: {key} | None = Field(default=None, foreign_key="Team"{stored})',
            'players: Relation[list["Player"]] = Reverse("mentor_id")',
        ],
    )
    team_reverse = 'players: Relation[list["Player"]] = Reverse("team_id")'
    cases["reverse on Team with no column"] = _case(
        "reverse", "players", local=False, owner="Team", Team=[team_reverse]
    )
    cases["reverse on Team over a column pointing elsewhere"] = _case(
        "reverse",
        "players",
        local=False,
        owner="Team",
        Player=[
            f'mentor_id: {key} | None = Field(default=None, foreign_key="Player"{stored})'
        ],
        Team=['players: Relation[list["Player"]] = Reverse("mentor_id")'],
    )
    cases["reverse on Team naming a missing column, one key pointing back"] = _case(
        "reverse",
        "players",
        local=True,
        owner="Team",
        Player=[f"team_id: {key} = {fk('Team')}"],
        Team=['players: Relation[list["Player"]] = Reverse("missing_id")'],
    )
    tournaments = (
        'tournaments: Relation[list["Tournament"]] = '
        'ManyToMany(through="player_tournaments")'
    )
    elsewhere = 'players: Relation[list["Player"]] = Reverse("tournaments")'
    cases["reverse on Team over a many-to-many to Tournament"] = _case(
        "reverse",
        "players",
        local=True,
        owner="Team",
        Player=[tournaments],
        Team=[elsewhere],
    )
    cases["reverse on Team over a many-to-many, a key pointing back"] = _case(
        "reverse",
        "players",
        local=True,
        owner="Team",
        Player=[tournaments, f"team_id: {key} = {fk('Team')}"],
        Team=[elsewhere],
    )
    cases["reverse on Player over its own many-to-many, a self key"] = _case(
        "reverse",
        "players",
        local=True,
        Player=[
            tournaments,
            f'mentor_id: {key} | None = Field(default=None, foreign_key="Player"{stored})',
            elsewhere,
        ],
    )
    cases["reverse on Player over its own many-to-many, two self keys"] = _case(
        "reverse",
        "players",
        local=True,
        Player=[
            tournaments,
            f'mentor_id: {key} | None = Field(default=None, foreign_key="Player"{stored})',
            f'coach_id: {key} | None = Field(default=None, foreign_key="Player"{stored})',
            elsewhere,
        ],
    )
    cases["reverse on Player over its own many-to-many"] = _case(
        "reverse", "players", local=False, Player=[tournaments, elsewhere]
    )
    spellings = {
        "with no through": 'Relation[list["{t}"]] = ManyToMany()',
        "under | None": 'Relation[list["{t}"]] | None = ManyToMany(through="player_links")',
        "with | None inside": (
            'Relation[list["{t}"] | None] = ManyToMany(through="player_links")'
        ),
        "annotated as the model": '"{t}" = ManyToMany(through="player_links")',
        "in Annotated metadata": (
            'Annotated[Relation[list["{t}"]], ManyToMany(through="player_links")]'
        ),
        "through a dashed name": 'Relation[list["{t}"]] = ManyToMany(through="post-tags")',
        "through a 70-character name": (
            f'Relation[list["{{t}}"]] = ManyToMany(through="{"t" * 70}")'
        ),
        "as declared": 'Relation[list["{t}"]] = ManyToMany(through="player_links")',
    }
    for t in ("Tag", "Player"):
        for spelling, written in spellings.items():
            if spelling == "as declared" and t == "Tag":
                continue  # a working many-to-many, refused only for itself
            cases[f"many-to-many to {t} {spelling}"] = _case(
                "many-to-many",
                "links",
                local=t == "Tag",
                Player=[f"links: {written.format(t=t)}"],
            )
    cases["bare many-to-many a Reverse on Tag names"] = _case(
        "many-to-many",
        "links",
        local=True,
        Player=['links: Relation = ManyToMany(through="player_links")'],
        Tag=['linked: Relation[list["Player"]] = Reverse("links")'],
    )
    # Each way links one of the models; the other model's Reverse then
    # declares a many-to-many of its own.
    cases["bare many-to-many Reverses on Tag and Tournament name"] = _case(
        "many-to-many",
        "links",
        local=False,
        Player=['links: Relation = ManyToMany(through="player_links")'],
        Tag=['linked: Relation[list["Player"]] = Reverse("links")'],
        Tournament=['linked: Relation[list["Player"]] = Reverse("links")'],
    )
    cases["bare many-to-many a Reverse on Player names"] = _case(
        "many-to-many",
        "links",
        local=False,
        Player=[
            'links: Relation = ManyToMany(through="player_links")',
            'linked: Relation[list["Player"]] = Reverse("links")',
        ],
    )
    cases["many-to-many whose derived join table is too long"] = _case(
        "many-to-many",
        _LONG_FIELD,
        local=True,
        Player=[f'{_LONG_FIELD}: Relation[list["Tag"]] = ManyToMany()'],
    )
    # The fix's join table is free of every other declaration: here the name
    # the long field's derived one shortens to is taken already.
    shortened = f"player_{_LONG_FIELD}"[:63]
    cases["many-to-many whose shortened join table another one holds"] = _case(
        "many-to-many",
        f"{_LONG_FIELD}_b",
        local=True,
        Player=[
            f'{_LONG_FIELD}: Relation[list["Tag"]] = ManyToMany(through="{shortened}")',
            f'{_LONG_FIELD}_b: Relation[list["Tournament"]] = ManyToMany()',
        ],
    )
    cases["two many-to-manys declaring one join table"] = _case(
        "many-to-many",
        "others",
        local=True,
        Player=[
            'links: Relation[list["Tag"]] = ManyToMany(through="player_links")',
            'others: Relation[list["Tournament"]] = ManyToMany(through="player_links")',
        ],
    )
    cases["many-to-many whose join table is a model's table"] = _case(
        "many-to-many",
        "links",
        local=True,
        Player=['links: Relation[list["Tag"]] = ManyToMany(through="team")'],
    )
    # A join table's renamed_from that cannot be its previous name.
    for spelling, previous in {
        "its own join table": "player_links",
        "a name that is no table": "player-links",
    }.items():
        cases[f"many-to-many renamed from {spelling}"] = _case(
            "many-to-many",
            "links",
            local=True,
            Player=[
                'links: Relation[list["Tag"]] = '
                f'ManyToMany(through="player_links", renamed_from="{previous}")'
            ],
        )
    # The key shared with the primary key, and every relation that names it.
    cases["key shared with the primary key"] = _case(
        "shared key",
        "id",
        local=False,
        no_pk=("Player",),
        Player=[
            f'id: {key} = Field(primary_key=True, foreign_key="Team"{stored})',
            'team: Relation["Team"] = Forward("id")',
        ],
        Team=['profiles: Relation[list["Player"]] = Reverse("id")'],
    )
    cases["forward over the primary key"] = _case(
        "forward",
        "team",
        local=False,
        Player=[
            'team: Relation["Team"] = Forward('
            + ('"id"' if pk.startswith("id") else '"code"')
            + ")"
        ],
    )
    # A key that is the primary key, with a key column on Card pointing at it:
    # the new primary key keeps its type, so Card's column still fits.
    primary = f'team_id: {key} = Field(primary_key=True, foreign_key="Team"{stored})'
    card = f"player_id: {key} = {fk('Player')}"
    cases["primary-key key"] = _case(
        "key", "team_id", local=False, no_pk=("Player",), Player=[primary], Card=[card]
    )
    if storage == "int":
        cases["primary-key key beside a plain id"] = _case(
            "key",
            "team_id",
            local=False,
            no_pk=("Player",),
            Player=["id: int = 0", primary],
            Card=[card],
        )
    # Team has no primary key and two key columns of different types point at
    # it: the fix gives Team the refused one's type, and retypes the other.
    # On one model, the model being defined is seen whether Team is written
    # as a class (refused when Player is defined) or by name.
    cases["keys of two types to a model with no primary key"] = _case(
        "key",
        ("team_id", "backup_id"),
        local=False,
        no_pk=("Team",),
        Team=['name: str = ""'],
        Player=[
            f'team_id: {key} = Field(foreign_key="Team"{stored})',
            f'backup_id: {wrong} = Field(foreign_key="Team")',
        ],
    )
    # On two models, Player defined before Card: refused when relations
    # resolve, with both columns known.
    cases["keys of two types on two models to a model with no primary key"] = _case(
        "key",
        "team_id",
        local=False,
        owner=("Player", "Card"),
        no_pk=("Team",),
        orders=("Player first",),
        Player=[f'team_id: {key} = Field(foreign_key="Team"{stored})'],
        Card=[f'team_id: {wrong} = Field(foreign_key="Team")'],
    )
    # A bare forward over a key column Player inherits: the class body names
    # no target, so relation resolution writes the line.
    cases["bare forward over an inherited key column"] = _case(
        "forward",
        "team",
        local=True,
        bases={"Player": "Card"},
        orders=("Player last",),
        Card=[f"team_id: {key} = {fk('Team')}"],
        Player=['team: Relation = Forward("team_id")'],
    )
    return cases


def _source(models: dict, case: dict, storage: str, order: str) -> str:
    pk = _KEYS[storage][0]
    classes = []
    # A model a fix adds is written after the others, with only its own lines.
    names = _ORDERS[order] + tuple(name for name in models if name not in _MODELS)
    for model in names:
        # A subclass inherits its base's primary key.
        own_pk = model in _MODELS and model not in (*case["no_pk"], *case["bases"])
        body = ([pk] if own_pk else []) + models.get(model, []) or ["pass"]
        base = case["bases"].get(model, "Model")
        classes.append(
            f"class {model}({base}):\n" + "".join(f"    {line}\n" for line in body)
        )
    return (
        "from ferro import Forward, Reverse\n\n"
        + "\n".join(classes)
        + "".join(f"\n{line}\n" for line in case["after"])
    )


def _as_classes(models: dict) -> dict:
    """``models`` with Team written as the class, not its name, by every model
    defined after it (in "Player last" order, every other model)."""
    return {
        name: [line.replace('foreign_key="Team"', "foreign_key=Team") for line in lines]
        if name != "Team"
        else list(lines)
        for name, lines in models.items()
    }


def _field_of(line: str) -> str:
    return line.split(":", 1)[0].strip()


def _apply(models: dict, pk: str, no_pk: tuple, edits: tuple[Edit, ...]) -> dict:
    """``models`` with ``edits`` applied: a field's line replaced in its
    class, a line added to it (or a model added) or a field removed."""
    fixed = {name: list(lines) for name, lines in models.items()}
    for edit in edits:
        if not edit.field:
            assert edit.model not in _MODELS and edit.model not in fixed, edit
            fixed[edit.model] = list(edit.lines)
            continue
        lines = fixed.setdefault(edit.model, [])
        if edit.model not in no_pk and not any(
            _field_of(line) == _field_of(pk) for line in lines
        ):
            lines.insert(0, pk)  # the primary key `_source` writes
        names = [_field_of(line) for line in lines]
        if edit.action == "add":
            assert edit.field not in names, edit
            lines.extend(edit.lines)
        elif edit.action == "replace":
            index = names.index(edit.field)
            lines[index : index + 1] = edit.lines
        else:
            del lines[names.index(edit.field)]
    return fixed


def _refused(tmp_path, source: str, future: bool) -> DeclarationRefusal:
    _wipe()
    try:
        with pytest.raises(DeclarationRefusal) as refusal:
            _import(tmp_path, source, future=future)
            ferro.ensure_resolved_modelset()
    finally:
        _wipe()
    return refusal.value


async def _links_rows(join: type, owner: type) -> None:
    """A join model a fix added links two rows of ``owner``, read back."""
    columns = join.__ferro_columns__
    assert {
        name: column.foreign_key.to_table
        for name, column in columns.items()
        if column.foreign_key is not None
    } == {"player_id": owner.__ferro_table__, "other_player_id": owner.__ferro_table__}
    await ferro.connect("sqlite::memory:", auto_migrate=True)
    async with ferro.engines.session():
        pk = owner.__ferro_pk__
        # Explicit keys: an insert naming no column is not valid SQLite.
        keys = ({pk: 1}, {pk: 2}) if pk == "id" else ({pk: "a1"}, {pk: "b2"})
        first = await owner.create(**keys[0])
        second = await owner.create(**keys[1])
        await join.create(
            player_id=getattr(first, pk), other_player_id=getattr(second, pk)
        )
        (row,) = await join.all()
        assert (row.player_id, row.other_player_id) == (
            getattr(first, pk),
            getattr(second, pk),
        )


async def _works(mod, case: dict) -> None:
    """Every model loads with a primary key, every many-to-many has a join
    table of its own, and the fix gives the case's kind."""
    ferro.ensure_resolved_modelset()
    models = [
        model
        for model in vars(mod).values()
        if isinstance(model, type) and issubclass(model, Model) and model is not Model
    ]
    assert all(model.__ferro_pk__ for model in models), models
    many_to_manys = [
        relation
        for model in models
        for relation in model.__ferro_relations__.values()
        if isinstance(relation, ManyToManyRelation)
    ]
    assert len(REGISTRY.join_tables()) == len(many_to_manys), REGISTRY.join_tables()
    field = case["field"]
    joins = [model for model in models if model.__name__ not in _MODELS]
    if joins:
        (join,) = joins
        await _links_rows(join, mod.Player)
        return
    if case["kind"] == "key":
        columns = [
            column
            for model in models
            for column in model.__ferro_columns__.values()
            if column.foreign_key is not None
        ]
        assert columns
    elif case["kind"] == "shared key":
        columns = mod.Player.__ferro_columns__
        assert mod.Player.__ferro_pk__ == "id"
        assert columns["id"].foreign_key is None
        assert columns["team_id"].foreign_key.to_table == "team"
        assert columns["team_id"].unique
        assert mod.Player.__ferro_relation_specs__["team"].fk_column == "team_id"
        assert mod.Team.__ferro_reverse_specs__["profiles"].child_fk_column == "team_id"
    elif case["kind"] == "forward":
        assert field in getattr(mod, case["owner"]).__ferro_relation_specs__
    elif case["kind"] in ("reverse", "many-to-many"):
        found = [
            model.__ferro_reverse_specs__[field]
            for model in models
            if field in model.__ferro_reverse_specs__
        ]
        assert found, f"no relation {field}"
        if case["kind"] == "many-to-many":
            assert found[0].is_m2m


def _sweep(storage: str) -> list[tuple[str, str, str]]:
    """Each case's name, definition order and target form for ``storage``."""
    runs = []
    for name, case in _cases(storage).items():
        for order in case["orders"]:
            runs.append((name, order, "name"))
            # A class is only written where it is already defined.
            if order == "Player last" and _as_classes(case["models"]) != case["models"]:
                runs.append((name, order, "class"))
    return runs


_SWEEP_CASES = [
    (storage, name, order, form, future)
    for storage in _KEYS
    for name, order, form in _sweep(storage)
    for future in (False, True)
]


def _spelled_by_name(
    alternatives: tuple[tuple[Edit, ...], ...],
) -> tuple[tuple[Edit, ...], ...]:
    """``alternatives`` with Team written by name wherever a kept line wrote it
    as the class."""
    return tuple(
        tuple(
            Edit(
                edit.model,
                edit.field,
                edit.action,
                tuple(
                    line.replace("foreign_key=Team", 'foreign_key="Team"')
                    for line in edit.lines
                ),
            )
            for edit in way
        )
        for way in alternatives
    )


def _refused_case(tmp_path, storage, case, order, form, future) -> DeclarationRefusal:
    models = _as_classes(case["models"]) if form == "class" else case["models"]
    return _refused(
        tmp_path,
        _source(models, case, storage, order),
        future,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("storage", "name", "order", "form", "future"), _SWEEP_CASES)
async def test_every_refusal_carries_a_fix_that_works(
    tmp_path, storage, name, order, form, future
):
    case = _cases(storage)[name]
    refusal = _refused_case(tmp_path, storage, case, order, form, future)
    message = str(refusal)

    # Which of two refused declarations is met first depends on the order.
    owners = case["owner"] if isinstance(case["owner"], tuple) else (case["owner"],)
    fields = case["field"] if isinstance(case["field"], tuple) else (case["field"],)
    assert refusal.model in owners and refusal.field in fields, message
    assert message == render_fix(
        refusal.model, refusal.field, refusal.reason, alternatives=refusal.alternatives
    )
    if case["local"] is None:
        assert refusal.alternatives == (), message
        assert "Write: " not in message and "Fix it by hand" not in message, message
        return
    assert refusal.alternatives, message
    if "Write: " in message:
        assert all(
            (edit.model, edit.field, edit.action)
            == (refusal.model, refusal.field, "replace")
            for way in refusal.alternatives
            for edit in way
        ), message
    assert ("Write: " in message) is case["local"], message
    assert ("Fix it by hand: (1) " in message) is not case["local"], message
    if form == "class":
        # Refused when the class is defined or when relations resolve, the
        # fix is the same, bar how the line it keeps spells the target.
        by_name = _refused_case(tmp_path, storage, case, order, "name", future)
        assert (refusal.model, refusal.field, refusal.reason) == (
            by_name.model,
            by_name.field,
            by_name.reason,
        )
        assert _spelled_by_name(refusal.alternatives) == by_name.alternatives

    models = _as_classes(case["models"]) if form == "class" else case["models"]
    for way in refusal.alternatives:
        fixed = _apply(models, _KEYS[storage][0], (*case["no_pk"], *case["bases"]), way)
        source = _source(fixed, case, storage, order)
        _wipe()
        try:
            await _works(_import(tmp_path, source, future=future), case)
        except Exception as error:
            raise AssertionError(
                f"the fix {way}\nfrom\n{message}\ndoes not work:\n{source}\n{error!r}"
            ) from error
        finally:
            _wipe()


# -- every refusal is swept --------------------------------------------------------
#
# Each place in src/ferro that builds a `DeclarationRefusal`, directly or
# through a function that returns one, must be on the stack of a refusal the
# sweep meets, so a new refusal without a sweep case fails here, however it
# is raised.


def _construction_sites_in_source() -> set[tuple[str, int]]:
    """Every call in src/ferro that builds a `DeclarationRefusal`: (file, line).

    A call of the class or of a function annotated to return one; the class's
    own module, which builds one only to unpickle it, is left out.
    """
    root = Path(ferro.__file__).parent
    trees = {
        path: ast.parse(path.read_text())
        for path in root.rglob("*.py")
        if path != root / "relations" / "fixes.py"
    }
    builders = {"DeclarationRefusal"} | {
        node.name
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and isinstance(node.returns, ast.Name)
        and node.returns.id == "DeclarationRefusal"
    }
    sites = set()
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = called.id if isinstance(called, ast.Name) else None
            if isinstance(called, ast.Attribute):
                name = called.attr
            if name in builders:
                sites.add((str(path.relative_to(root)), node.lineno))
    return sites


def test_the_sweep_reaches_every_refusal(tmp_path, monkeypatch):
    root = Path(ferro.__file__).parent
    met: set[tuple[str, int]] = set()
    build = DeclarationRefusal.__init__

    def recording(self, *args, **kwargs):
        # Every line of src/ferro on the stack while the refusal is built.
        frame = sys._getframe(1)
        while frame is not None:
            path = Path(frame.f_code.co_filename)
            if path.is_relative_to(root):
                met.add((str(path.relative_to(root)), frame.f_lineno))
            frame = frame.f_back
        build(self, *args, **kwargs)

    monkeypatch.setattr(DeclarationRefusal, "__init__", recording)
    for storage in _KEYS:
        for name, order, form in _sweep(storage):
            _refused_case(tmp_path, storage, _cases(storage)[name], order, form, False)

    assert _construction_sites_in_source() - met == set()


# -- the cases the sweep was widened for ----------------------------------------------


def _player(tmp_path, body: str) -> DeclarationRefusal:
    return _refused(
        tmp_path,
        "from ferro import Forward\n\n"
        "class Team(Model):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n\n" + body,
        future=False,
    )


def test_a_set_null_key_a_forward_reads_is_fixed_on_both_declarations(tmp_path):
    refusal = _player(
        tmp_path,
        "class Player(Model):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n"
        '    team_id: int = Field(foreign_key=Team, on_delete="SET NULL")\n'
        '    team: Relation[Team] = Forward("team_id")\n',
    )

    assert str(refusal) == (
        'Player.team_id: on_delete="SET NULL" needs a nullable column. Fix it by '
        "hand: (1) on Player, replace team_id with `team_id: int | None = "
        'Field(default=None, foreign_key=Team, on_delete="SET NULL")`; (2) on '
        "Player, replace team with "
        '`team: Relation[Team | None] = Forward("team_id")`'
    )


def test_a_set_null_key_nothing_reads_is_a_write_line(tmp_path):
    refusal = _player(
        tmp_path,
        "class Player(Model):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n"
        '    team_id: int = Field(foreign_key=Team, on_delete="SET NULL")\n',
    )

    assert str(refusal) == (
        'Player.team_id: on_delete="SET NULL" needs a nullable column. Write: '
        'team_id: int | None = Field(default=None, foreign_key=Team, on_delete="SET NULL")'
    )


def test_a_str_key_to_a_keyless_class_is_refused_with_the_same_fix(tmp_path):
    refusal = _refused(
        tmp_path,
        "class Team(Model):\n"
        "    name: str = ''\n\n"
        "class Player(Model):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n"
        "    team_id: str = Field(foreign_key=Team)\n",
        future=False,
    )

    assert refusal.edits == (
        Edit("Team", "id", "add", ("id: str = Field(primary_key=True)",)),
    )


def test_a_str_key_to_a_model_with_no_primary_key_gives_it_a_str_one(tmp_path):
    refusal = _refused(
        tmp_path,
        "class Player(Model):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n"
        '    team_id: str = Field(foreign_key="Team")\n\n'
        "class Team(Model):\n"
        "    name: str = ''\n",
        future=False,
    )

    assert refusal.edits == (
        Edit("Team", "id", "add", ("id: str = Field(primary_key=True)",)),
    )


def test_a_primary_key_key_pointed_at_keeps_its_type_on_the_new_key(tmp_path):
    refusal = _refused(
        tmp_path,
        "class Team(Model):\n"
        "    id: str = Field(primary_key=True)\n\n"
        "class Player(Model):\n"
        "    team_id: str = Field(primary_key=True, foreign_key=Team)\n\n"
        "class Card(Model):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n"
        "    player_id: str = Field(foreign_key=Player)\n",
        future=False,
    )

    assert refusal.edits == (
        Edit("Player", "id", "add", ("id: str = Field(primary_key=True)",)),
        Edit(
            "Player",
            "team_id",
            "replace",
            ("team_id: str = Field(foreign_key=Team, unique=True)",),
        ),
    )


def test_a_bare_many_to_many_nothing_names_carries_no_edits(tmp_path):
    refusal = _refused(
        tmp_path,
        "class Post(Model):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n"
        '    tags: Relation = ManyToMany(through="post_tags")\n',
        future=False,
    )

    assert refusal.edits == ()
    assert "Write: " not in str(refusal) and "Fix it by hand" not in str(refusal)
