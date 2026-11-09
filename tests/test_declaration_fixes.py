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
declaration; any other fix is numbered steps.
"""

import pytest

import ferro
from ferro import Model
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


def test_a_refusal_with_no_edits_is_its_reason():
    assert render_fix("Post", "tags", "say what", []) == "Post.tags: say what"


# -- the sweep ----------------------------------------------------------------------
#
# Each case is a refused modelset: the lines of each model, the refused
# declaration (`owner`.`field`), what its fix must give (`kind`) and whether
# the fix is local (a `Write:` line). It runs over the target key's storage,
# both definition orders and both __future__ modes; most cases also run with
# the linked model another model or the owner, and with a relation reading
# the refused column or none. The carried edits are applied to the source
# with one helper, and the result must load, every model with a primary key.

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


def _case(kind, field, *, local, owner="Player", no_pk=(), **models):
    """One refused modelset; ``models`` are the lines of each model."""
    return {
        "kind": kind,
        "owner": owner,
        "field": field,
        "local": local,
        "no_pk": no_pk,
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
        cases[f"{name} to {t}"] = _case(
            "key", "team_id", local=local, no_pk=no_pk, Player=[line]
        )
        # A reader makes SET NULL reach the relation: two declarations change.
        cases[f"{name} to {t}, read by a forward relation"] = _case(
            "key",
            "team_id",
            local=local and name != "required SET NULL key",
            no_pk=no_pk,
            Player=[line, reader],
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
    cases["keys of two types to a model with no primary key"] = _case(
        "key",
        "team_id",
        local=False,
        owner=("Player", "Card"),
        no_pk=("Team",),
        Player=[f'team_id: {key} = Field(foreign_key="Team"{stored})'],
        Card=[f'team_id: {wrong} = Field(foreign_key="Team")'],
    )
    return cases


def _source(models: dict, no_pk: tuple, storage: str, order: str) -> str:
    pk = _KEYS[storage][0]
    classes = []
    # A model a fix adds is written after the others, with only its own lines.
    names = _ORDERS[order] + tuple(name for name in models if name not in _MODELS)
    for model in names:
        body = [] if model in no_pk or model not in _MODELS else [pk]
        body += models.get(model, []) or ["pass"]
        classes.append(
            f"class {model}(Model):\n" + "".join(f"    {line}\n" for line in body)
        )
    return "from ferro import Forward, Reverse\n\n" + "\n".join(classes)


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


def _works(mod, case: dict) -> None:
    """Every model loads with a primary key, and the fix gives the case's kind."""
    ferro.ensure_resolved_modelset()
    models = [
        model
        for model in vars(mod).values()
        if isinstance(model, type) and issubclass(model, Model) and model is not Model
    ]
    assert all(model.__ferro_pk__ for model in models), models
    field = case["field"]
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
        joins = [model for model in models if model.__name__ not in _MODELS]
        assert found or joins, f"no relation {field}"
        if case["kind"] == "many-to-many" and found:
            assert found[0].is_m2m


_SWEEP_CASES = [
    (storage, name, order, future)
    for storage in _KEYS
    for name in _cases(storage)
    for order in _ORDERS
    for future in (False, True)
]


@pytest.mark.parametrize(("storage", "name", "order", "future"), _SWEEP_CASES)
def test_every_refusal_carries_a_fix_that_works(tmp_path, storage, name, order, future):
    case = _cases(storage)[name]
    refusal = _refused(
        tmp_path, _source(case["models"], case["no_pk"], storage, order), future
    )
    message = str(refusal)

    # Which of two refused models resolution meets first depends on the order.
    owners = case["owner"] if isinstance(case["owner"], tuple) else (case["owner"],)
    assert refusal.model in owners and refusal.field == case["field"], message
    assert message == render_fix(
        refusal.model, refusal.field, refusal.reason, refusal.edits
    )
    assert refusal.edits, message
    if "Write: " in message:
        assert all(
            (edit.model, edit.field, edit.action)
            == (refusal.model, refusal.field, "replace")
            for edit in refusal.edits
        ), message
    assert ("Write: " in message) is case["local"], message
    assert ("Fix it by hand: (1) " in message) is not case["local"], message

    pk = _KEYS[storage][0]
    fixed = _apply(case["models"], pk, case["no_pk"], refusal.edits)
    source = _source(fixed, case["no_pk"], storage, order)
    _wipe()
    try:
        _works(_import(tmp_path, source, future=future), case)
    except Exception as error:
        raise AssertionError(
            f"the fix in\n{message}\ndoes not work:\n{source}\n{error!r}"
        ) from error
    finally:
        _wipe()


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
