"""Foreign-key columns declared with `Field(foreign_key=...)`, and the
`Forward` / `Reverse` relations that name them (#876).

```python
class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    players: Relation[list["Player"]] = Reverse("team_id")

class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team, index=True)
    team: Relation[Team] = Forward("team_id")
```

It compiles to the table, constraint and snapshot the removed
`Annotated[Team, ForeignKey(...)]` form compiled to.
"""

import json
from pathlib import Path
from typing import Annotated, Literal, NewType

import pytest

import ferro
from ferro import (
    Check,
    FerroField,
    Field,
    Forward,
    Model,
    Relation,
    Reverse,
    clear_registry,
    ensure_resolved_modelset,
    reset_engine,
)
from ferro.columns import ForeignKeyRef
from ferro.registry import REGISTRY
from ferro.relations import resolve_relationships


@pytest.fixture(autouse=True)
def _clean(request):
    """An empty registry for the tests that define their own models; the
    database tests read the module's models."""
    if request.node.get_closest_marker("backend_matrix") is None:
        request.getfixturevalue("clean_registry")
    yield


def _wipe() -> None:
    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


def _snapshot() -> str:
    """The resolved modelset's payload, as `ferro migrate new` records it."""
    return json.dumps(ensure_resolved_modelset()["payload"], sort_keys=True, indent=2)


# -- the column -------------------------------------------------------------------


def test_a_key_column_carries_its_foreign_key_and_the_relation_names_it():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key=Team, index=True)
        team: Relation[Team] = Forward("team_id")

    assert Player.__ferro_columns__["team_id"].foreign_key == ForeignKeyRef(
        to_table="team", on_delete="CASCADE"
    )
    assert Player.__ferro_relation_specs__["team"].fk_column == "team_id"


def test_the_annotated_key_column_is_the_assignment_one():
    def define(annotated: bool):
        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)

        if annotated:

            class Player(Model):
                id: int | None = Field(default=None, primary_key=True)
                team_id: Annotated[int, Field(foreign_key=Team, index=True)]
                team: Relation[Team] = Forward("team_id")

        else:

            class Player(Model):
                id: int | None = Field(default=None, primary_key=True)
                team_id: int = Field(foreign_key=Team, index=True)
                team: Relation[Team] = Forward("team_id")

        return Player.__ferro_columns__["team_id"]

    assigned = define(annotated=False)
    _wipe()
    annotated = define(annotated=True)

    assert annotated == assigned
    assert annotated.foreign_key == ForeignKeyRef(to_table="team", on_delete="CASCADE")


def test_ferro_field_takes_the_foreign_key_options():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: Annotated[
            int | None, FerroField(foreign_key=Team, on_delete="SET NULL")
        ] = None

    assert Player.__ferro_fields__["team_id"].foreign_key is Team
    assert Player.__ferro_fields__["team_id"].on_delete == "SET NULL"
    assert Player.__ferro_columns__["team_id"].foreign_key == ForeignKeyRef(
        to_table="team", on_delete="SET NULL"
    )


# What `Annotated[Team, ForeignKey(...)]` compiled to for each key below,
# recorded before the old form was removed.
OLD_FORM_SNAPSHOTS = Path(__file__).parent / "fixtures" / "old_form_key_snapshots.json"


def _define(key: str) -> None:
    """Declare one key in the explicit-column form."""
    if key == "required":

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Relation[list["Player"]] = Reverse("team_id")

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            team: Relation[Team] = Forward("team_id")

    elif key == "set-null":

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Relation[list["Player"]] = Reverse("team_id")

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int | None = Field(
                default=None, foreign_key=Team, on_delete="SET NULL"
            )
            team: Relation[Team | None] = Forward("team_id")

    elif key == "indexed":

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Relation[list["Player"]] = Reverse("team_id")

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team, index=True)
            team: Relation[Team] = Forward("team_id")

    elif key == "unique":

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            captain: Relation[list["Player"]] = Reverse("team_id")

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team, unique=True)
            team: Relation[Team] = Forward("team_id")

    else:

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            mentor_id: int | None = Field(
                default=None, foreign_key="Player", on_delete="SET NULL"
            )
            mentor: Relation["Player | None"] = Forward("mentor_id")
            mentees: Relation[list["Player"]] = Reverse("mentor_id")


@pytest.mark.parametrize(
    "key", ["required", "set-null", "indexed", "unique", "self-reference"]
)
def test_the_new_form_compiles_to_the_old_forms_snapshot(key):
    _define(key)

    assert json.loads(_snapshot()) == json.loads(OLD_FORM_SNAPSHOTS.read_text())[key]


def test_the_key_column_name_is_free():
    class User(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_user_id: int = Field(foreign_key=User)
        owner: Relation[User] = Forward("owner_user_id")

    post = next(
        model
        for model in ensure_resolved_modelset()["payload"]["models"]
        if model["table_name"] == "post"
    )
    assert [
        (foreign_key["name"], foreign_key["column"])
        for foreign_key in post["foreign_keys"]
    ] == [("fk_post_owner_user_id_user", "owner_user_id")]


def test_a_self_reference_resolves_by_class_name():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        mentor_id: int | None = Field(default=None, foreign_key="Player")
        mentor: Relation["Player | None"] = Forward("mentor_id")

    resolve_relationships()

    assert Player.__ferro_columns__["mentor_id"].foreign_key == ForeignKeyRef(
        to_table="player", on_delete="CASCADE"
    )
    assert Player.__ferro_relation_specs__["mentor"].target is Player


UserId = NewType("UserId", int)


def test_a_new_type_over_the_target_key_type_is_accepted_and_keeps_its_storage():
    class User(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: UserId = Field(foreign_key=User)

    resolve_relationships()
    column = next(
        column
        for model in ensure_resolved_modelset()["payload"]["models"]
        if model["table_name"] == "post"
        for column in model["columns"]
        if column["name"] == "owner_id"
    )
    for dialect in ("postgres", "sqlite"):
        resolved = json.loads(
            ferro._core._resolve_storage_type(json.dumps(column), dialect)
        )
        assert resolved == {"kind": "scalar", "token": "int"}


def test_a_key_column_takes_the_target_keys_db_type():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True, db_type="bigint")

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key=Team, db_type="bigint")

    assert Player.__ferro_columns__["team_id"].db_type == "bigint"


def test_a_retired_key_column_keeps_its_column_and_constraint():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int | None = Field(default=None, foreign_key=Team, retired=True)

    player = next(
        model
        for model in ensure_resolved_modelset()["payload"]["models"]
        if model["table_name"] == "player"
    )
    column = next(column for column in player["columns"] if column["name"] == "team_id")

    assert column["retired"] is True
    assert [foreign_key["name"] for foreign_key in player["foreign_keys"]] == [
        "fk_player_team_id_team"
    ]
    with pytest.raises(AttributeError, match="Player.team_id is retired"):
        Player().team_id  # noqa: B018


def test_a_retired_key_column_to_a_later_model_points_at_its_table():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: str | None = Field(default=None, foreign_key="Team", retired=True)

    class Team(Model):
        __ferro_table__ = "teams"
        code: str = Field(primary_key=True)

    player = next(
        model
        for model in ensure_resolved_modelset()["payload"]["models"]
        if model["table_name"] == "player"
    )

    assert [
        (key["to_table"], key["to_column"], key["name"])
        for key in player["foreign_keys"]
    ] == [("teams", "code", "fk_player_team_id_teams")]


def test_a_retired_key_column_to_a_model_never_defined_fails_resolution():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int | None = Field(default=None, foreign_key="Team", retired=True)

    with pytest.raises(RuntimeError, match="'Team' not found"):
        ensure_resolved_modelset()


def test_forward_and_reverse_are_public():
    assert {"Forward", "Reverse"} <= set(ferro.__all__)


def test_a_forward_relation_needs_no_reverse():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key=Team)
        team: Relation[Team] = Forward("team_id")

    resolve_relationships()

    assert Player.__ferro_relation_specs__["team"].target is Team


# -- the refusals --------------------------------------------------------------------


def _refused(define) -> str:
    with pytest.raises(TypeError) as refusal:
        define()
        resolve_relationships()
    return str(refusal.value)


def test_a_key_column_of_another_type_is_refused():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: str = Field(foreign_key=Team)

    assert _refused(define) == (
        "Player.team_id: Team.id is int, so its foreign-key column is int too. "
        "Write: team_id: int = Field(foreign_key=Team)"
    )


def test_a_subclass_of_the_target_key_type_is_refused():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: bool = Field(foreign_key=Team)

    assert _refused(define) == (
        "Player.team_id: Team.id is int, so its foreign-key column is int too. "
        "Write: team_id: int = Field(foreign_key=Team)"
    )


def test_a_key_column_stored_unlike_its_target_names_the_db_type_to_add():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True, db_type="bigint")

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)

    assert _refused(define) == (
        "Player.team_id: Team.id is stored as bigint and this column as int. "
        'Write: team_id: int = Field(foreign_key=Team, db_type="bigint")'
    )


def test_set_null_on_a_required_key_column_is_refused():
    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            mentor_id: int = Field(foreign_key="Player", on_delete="SET NULL")

    assert _refused(define) == (
        'Player.mentor_id: on_delete="SET NULL" needs a nullable column. '
        "Write: mentor_id: int | None = Field(default=None, "
        'foreign_key="Player", on_delete="SET NULL")'
    )


def test_set_null_beside_nullable_false_is_refused_for_the_nullable():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int | None = Field(
                foreign_key=Team, on_delete="SET NULL", nullable=False
            )

    assert _refused(define) == (
        "Player.team_id: a foreign-key column is nullable when its annotation "
        "allows None, so it takes no nullable=. Write: team_id: int | None = "
        'Field(default=None, foreign_key=Team, on_delete="SET NULL")'
    )


def test_nullable_false_on_a_key_column_moves_into_the_annotation():
    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int | None = Field(foreign_key="Team", nullable=False)

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)

    assert _refused(define) == (
        "Player.team_id: a foreign-key column is nullable when its annotation "
        "allows None, so it takes no nullable=. "
        'Write: team_id: int = Field(foreign_key="Team")'
    )


def test_nullable_true_on_a_key_column_moves_into_the_annotation():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: Annotated[int, FerroField(foreign_key=Team, nullable=True)]

    assert _refused(define) == (
        "Player.team_id: a foreign-key column is nullable when its annotation "
        "allows None, so it takes no nullable=. "
        "Write: team_id: int | None = Field(default=None, foreign_key=Team)"
    )


def test_a_nullable_fix_rewrites_the_relation_reading_the_column():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team, nullable=True)
            team: Relation[Team] = Forward("team_id")

    assert _refused(define) == (
        "Player.team_id: a foreign-key column is nullable when its annotation "
        "allows None, so it takes no nullable=. Fix it by hand: (1) on Player, "
        "replace team_id with `team_id: int | None = Field(default=None, "
        "foreign_key=Team)`; (2) on Player, replace team with "
        '`team: Relation[Team | None] = Forward("team_id")`'
    )


def test_on_delete_without_a_foreign_key_is_refused():
    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            note: str | None = Field(default=None, on_delete="SET NULL")

    assert _refused(define) == (
        "Player.note: on_delete is a foreign-key option and this column has no "
        "foreign_key. Write: note: str | None = Field(default=None)"
    )


def test_a_key_column_that_is_the_primary_key_is_refused():
    class User(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Profile(Model):
            user_id: int | None = Field(
                default=None, primary_key=True, foreign_key=User
            )

    assert _refused(define) == (
        "Profile.user_id: a foreign-key column cannot be the primary key. "
        "Fix it by hand: (1) on Profile, add `id: int | None = Field(default=None, "
        "primary_key=True)`; (2) on Profile, replace user_id with `user_id: int "
        "| None = Field(default=None, foreign_key=User, unique=True)`"
    )


def test_a_generated_key_column_is_refused():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            number: int = 0
            team_id: int = Field(
                foreign_key=Team, generated=lambda player: player.number
            )

    assert _refused(define) == (
        "Player.team_id: a foreign-key column cannot be generated. "
        "Write: team_id: int = Field(foreign_key=Team)"
    )


def test_a_relation_over_a_column_without_a_foreign_key_is_refused():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int
            team: Relation[Team] = Forward("team_id")

    assert _refused(define) == (
        "Player.team: team_id has no foreign_key. Fix it by hand: (1) on Player, "
        'replace team_id with `team_id: int = Field(foreign_key="Team")`'
    )


def test_a_relation_over_a_missing_column_names_the_one_key_to_its_target():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            team: Relation[Team] = Forward("teamid")

    assert _refused(define) == (
        "Player.team: Player has no column teamid. "
        'Write: team: Relation[Team] = Forward("team_id")'
    )


def test_a_relation_over_a_missing_column_with_no_key_names_the_column_to_add():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team: Relation[Team] = Forward("teamid")

    assert _refused(define) == (
        "Player.team: Player has no column teamid. Fix it by hand: (1) on "
        "Player, add `teamid: int = Field(foreign_key=Team)`"
    )


def test_a_relation_to_another_model_than_its_column_is_refused():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Club(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            team: Relation[Club] = Forward("team_id")

    assert _refused(define) == (
        "Player.team: team_id points at Team, not Club. "
        'Write: team: Relation[Team] = Forward("team_id")'
    )


def test_a_required_relation_over_a_nullable_column_is_refused():
    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            mentor_id: int | None = Field(default=None, foreign_key="Player")
            mentor: Relation["Player"] = Forward("mentor_id")

    assert _refused(define) == (
        "Player.mentor: mentor_id is nullable, so the relation is too. "
        'Write: mentor: Relation["Player | None"] = Forward("mentor_id")'
    )


def test_a_nullable_relation_over_a_required_column_is_refused():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            team: Relation[Team | None] = Forward("team_id")

    assert _refused(define) == (
        "Player.team: team_id is required, so the relation is too. "
        'Write: team: Relation[Team] = Forward("team_id")'
    )


def test_a_reverse_over_a_key_that_does_not_point_back_names_the_one_that_does():
    def define():
        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Relation[list["Player"]] = Reverse("mentor_id")

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            mentor_id: int | None = Field(default=None, foreign_key="Player")

    assert _refused(define) == (
        "Team.players: Player.mentor_id points at Player, not Team. "
        'Write: players: Relation[list["Player"]] = Reverse("team_id")'
    )


def test_a_reverse_with_no_key_pointing_back_names_the_column_to_add():
    def define():
        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Relation[list["Player"]] = Reverse("mentor_id")

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            mentor_id: int | None = Field(default=None, foreign_key="Player")

    assert _refused(define) == (
        "Team.players: Player.mentor_id points at Player, not Team. Fix it by "
        'hand: (1) on Player, add `team_id: int = Field(foreign_key="Team")`; '
        "(2) on Team, replace players with "
        '`players: Relation[list["Player"]] = Reverse("team_id")`'
    )


def test_a_forward_relation_annotated_without_relation_is_refused():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            team: Team = Forward("team_id")

    assert _refused(define) == (
        "Player.team: a relation is annotated Relation[...]. "
        'Write: team: Relation[Team] = Forward("team_id")'
    )


def test_a_reverse_relation_annotated_without_relation_is_refused():
    def define():
        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: list["Player"] = Reverse("team_id")  # noqa: F821

    assert _refused(define) == (
        "Team.players: a relation is annotated Relation[...]. "
        'Write: players: Relation[list["Player"]] = Reverse("team_id")'
    )


def test_a_relation_in_annotated_metadata_is_refused_as_an_assignment():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            team: Annotated[Relation[Team], Forward("team_id")]

    assert _refused(define) == (
        "Player.team: a relation is an assignment, not Annotated metadata. "
        'Write: team: Relation[Team] = Forward("team_id")'
    )


def test_a_bare_relation_annotation_names_its_columns_target():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int | None = Field(default=None, foreign_key=Team)
            team: Relation = Forward("team_id")

    assert _refused(define) == (
        "Player.team: a relation is annotated Relation[...]. "
        'Write: team: Relation[Team | None] = Forward("team_id")'
    )


def test_a_bare_reverse_relation_annotation_names_the_model_pointing_back():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key="Team")

    def define():
        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Relation = Reverse("team_id")

    assert _refused(define) == (
        "Team.players: a relation is annotated Relation[...]. "
        'Write: players: Relation[list["Player"]] = Reverse("team_id")'
    )


def test_a_relation_over_a_union_of_models_is_refused_in_either_order():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Club(Model):
        id: int | None = Field(default=None, primary_key=True)

    # typing caches `Relation[Team | Club]`, so `Relation[Club | Team]` can
    # come back as it: neither order may name a target.
    for _ in range(2):
        for refused in (Relation[Team | Club], Relation[Club | Team]):
            with pytest.raises(TypeError) as refusal:

                class Player(Model):
                    id: int | None = Field(default=None, primary_key=True)
                    team_id: int = Field(foreign_key=Team)
                    team: refused = Forward("team_id")  # type: ignore[valid-type]

            assert str(refusal.value) == (
                "Player.team: a relation is annotated Relation[...]. "
                'Write: team: Relation[Team] = Forward("team_id")'
            )


def test_a_bare_reverse_two_models_point_back_to_is_refused_with_the_class():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key="Team")

    class Coach(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key="Team")

    # Refused when the class is defined, the same refusal resolution gives.
    with pytest.raises(TypeError) as refusal:

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Relation = Reverse("team_id")

    assert str(refusal.value) == (
        "Team.players: a relation is annotated Relation[...], and Player and "
        "Coach each declare team_id pointing at Team. Write: "
        'players: Relation[list[Player]] = Reverse("team_id") or '
        'players: Relation[list[Coach]] = Reverse("team_id")'
    )


def test_a_reverse_in_annotated_metadata_is_refused_as_an_assignment():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key="Team")

    def define():
        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Annotated[Relation[list[Player]], Reverse("team_id")]

    assert _refused(define) == (
        "Team.players: a relation is an assignment, not Annotated metadata. "
        'Write: players: Relation[list[Player]] = Reverse("team_id")'
    )


def test_a_forward_relation_over_a_retired_column_goes_with_it():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int | None = Field(default=None, foreign_key=Team, retired=True)
            team: Relation[Team | None] = Forward("team_id")

    assert _refused(define) == (
        "Player.team: team_id is retired, so the relation is retired with it. "
        "Fix it by hand: (1) on Player, remove team"
    )


def test_a_reverse_relation_over_a_retired_column_goes_with_it():
    def define():
        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: Relation[list["Player"]] = Reverse("team_id")

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int | None = Field(default=None, foreign_key=Team, retired=True)

    assert _refused(define) == (
        "Team.players: Player.team_id is retired, so the relation is retired "
        "with it. Fix it by hand: (1) on Team, remove players"
    )


@pytest.mark.parametrize(
    ("column", "written"),
    [
        ("team_id: int = Field(foreign_key=Team)", "Team"),
        ("team_id: int | None = Field(default=None, foreign_key=Team)", "Team | None"),
    ],
)
def test_a_none_wrapped_relation_takes_its_nullability_from_the_column(column, written):
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    namespace = {
        "Field": Field,
        "Forward": Forward,
        "Model": Model,
        "Relation": Relation,
    }
    namespace["Team"] = Team

    def define():
        exec(
            "class Player(Model):\n"
            "    id: int | None = Field(default=None, primary_key=True)\n"
            f"    {column}\n"
            '    team: Relation[Team] | None = Forward("team_id")\n',
            namespace,
        )

    assert _refused(define) == (
        "Player.team: a relation is annotated Relation[...], with no | None "
        f'around it. Write: team: Relation[{written}] = Forward("team_id")'
    )


@pytest.mark.parametrize(
    ("column", "written"),
    [
        ("team_id: int = Field(foreign_key=Team)", "Team"),
        ("team_id: int | None = Field(default=None, foreign_key=Team)", "Team | None"),
    ],
)
def test_annotated_metadata_takes_its_nullability_from_the_column(column, written):
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    namespace = {
        "Annotated": Annotated,
        "Field": Field,
        "Forward": Forward,
        "Model": Model,
        "Relation": Relation,
        "Team": Team,
    }

    def define():
        exec(
            "class Player(Model):\n"
            "    id: int | None = Field(default=None, primary_key=True)\n"
            f"    {column}\n"
            '    team: Annotated[Relation[Team], Forward("team_id")] | None = None\n',
            namespace,
        )

    assert _refused(define) == (
        "Player.team: a relation is an assignment, not Annotated metadata. "
        f'Write: team: Relation[{written}] = Forward("team_id")'
    )


def test_a_literal_column_keeps_its_quotes_in_the_fix():
    def define():
        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            side: Literal["home", "away"] | None = Field(
                default=None, on_delete="CASCADE"
            )

    assert _refused(define) == (
        "Player.side: on_delete is a foreign-key option and this column has no "
        'foreign_key. Write: side: Literal["home", "away"] | None = '
        "Field(default=None)"
    )


def test_a_string_target_is_checked_when_relations_resolve():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: str = Field(foreign_key="Team")

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(TypeError) as refusal:
        resolve_relationships()

    assert str(refusal.value) == (
        "Player.team_id: Team.id is int, so its foreign-key column is int too. "
        'Write: team_id: int = Field(foreign_key="Team")'
    )


def test_a_refused_resolution_is_refused_again_on_every_later_one():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: str = Field(foreign_key="Team")

    class Team(Model):
        __ferro_table__ = "teams"
        id: int | None = Field(default=None, primary_key=True)
        players: Relation[list[Player]] = Reverse("team_id")

    for _ in range(2):
        with pytest.raises(TypeError, match="Player.team_id: Team.id is int"):
            ensure_resolved_modelset()
    assert REGISTRY.is_dirty()


def test_a_class_target_is_checked_when_the_class_is_defined():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(TypeError, match="Player.team_id: Team.id is int"):

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: str = Field(foreign_key=Team)


def test_a_refused_class_leaves_no_model_behind():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(TypeError):

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: str = Field(foreign_key=Team)

    assert [model.__name__ for model in REGISTRY.models().values()] == ["Team"]


# -- reading through the relations ------------------------------------------------


class FKCTeam(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    players: Relation[list["FKCPlayer"]] = Reverse("team_id")
    home_games: Relation[list["FKCGame"]] = Reverse("home_team_id")
    away_games: Relation[list["FKCGame"]] = Reverse("away_team_id")


class FKCPlayer(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    team_id: int = Field(foreign_key=FKCTeam, index=True)
    team: Relation[FKCTeam] = Forward("team_id")
    coach_player_id: int | None = Field(
        default=None, foreign_key="FKCPlayer", on_delete="SET NULL"
    )
    coach: Relation["FKCPlayer | None"] = Forward("coach_player_id")


class FKCGame(Model):
    id: int | None = Field(default=None, primary_key=True)
    home_team_id: int = Field(foreign_key=FKCTeam)
    home_team: Relation[FKCTeam] = Forward("home_team_id")
    away_team_id: int = Field(foreign_key=FKCTeam)
    away_team: Relation[FKCTeam] = Forward("away_team_id")


class FKCUser(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class FKCPost(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    owner_user_id: int = Field(foreign_key=FKCUser)
    owner: Relation[FKCUser] = Forward("owner_user_id")


async def _seed():
    owls = await FKCTeam.create(name="Owls")
    larks = await FKCTeam.create(name="Larks")
    await FKCTeam.create(name="Empty")
    ada = await FKCPlayer.create(name="Ada", team_id=owls.id)
    await FKCPlayer.create(name="Grace", team_id=larks.id)
    return owls, larks, ada


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_loading_a_forward_relation_gives_the_target(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        _, _, ada = await _seed()

        team = await ada.team.load()

        assert isinstance(team, FKCTeam)
        assert team.name == "Owls"


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_include_fills_a_forward_relation(db_url, statement_records):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()
        player = (
            await FKCPlayer.select()
            .include(lambda player: player.team)
            .where(lambda player: player.name == "Ada")
            .first()
        )
        before = len(statement_records)

        assert player.team.name == "Owls"
        assert len(statement_records) == before


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_reverse_relation_queries_the_rows_pointing_back(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        owls, _, _ = await _seed()

        players = await owls.players.all()
        with_players = await (
            FKCTeam.where(lambda team: team.players.exists()).order_by("id").all()
        )

        assert [player.name for player in players] == ["Ada"]
        assert [team.name for team in with_players] == ["Owls", "Larks"]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_lambda_traverses_a_forward_relation(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        players = await FKCPlayer.where(lambda player: player.team.name == "Owls").all()

        assert [player.name for player in players] == ["Ada"]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_relation_keyword_is_refused_naming_the_declared_key_column(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        owls, _, _ = await _seed()
        coach = await FKCPlayer.create(name="Ada", team_id=owls.id)

        with pytest.raises(TypeError) as refused:
            FKCPlayer(name="Linus", team_id=owls.id, coach=coach)
        assert str(refused.value) == (
            "FKCPlayer.coach is a relation. Pass the foreign-key column's value, "
            "coach_player_id=coach.id, or set f_k_c_player.coach = coach on the "
            "instance"
        )

        await FKCPlayer(name="Linus", team_id=owls.id, coach_player_id=coach.id).save()
        saved = await FKCPlayer.where(lambda player: player.name == "Linus").first()

        assert saved.coach_player_id == coach.id


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_two_keys_to_one_table_each_read_their_own_rows(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        owls, larks, _ = await _seed()
        await FKCGame.create(home_team_id=owls.id, away_team_id=larks.id)
        await FKCGame.create(home_team_id=larks.id, away_team_id=owls.id)
        await FKCGame.create(home_team_id=larks.id, away_team_id=owls.id)

        home = await owls.home_games.all()
        away = await owls.away_games.all()

        assert [game.away_team_id for game in home] == [larks.id]
        assert [game.home_team_id for game in away] == [larks.id, larks.id]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_include_fills_a_relation_over_a_freely_named_column(
    db_url, statement_records
):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        ada = await FKCUser.create(name="Ada")
        await FKCPost.create(title="Notes", owner_user_id=ada.id)
        post = await FKCPost.select().include(lambda post: post.owner).first()
        before = len(statement_records)

        assert post.owner.name == "Ada"
        assert len(statement_records) == before


def test_a_checks_null_test_on_a_relation_reads_its_declared_column():
    class Coached(Model):
        id: int | None = Field(default=None, primary_key=True)
        coach_player_id: int | None = Field(default=None, foreign_key="Coached")
        coach: Relation["Coached | None"] = Forward("coach_player_id")

        __ferro_checks__ = (
            Check("no_coach", lambda coached: coached.coach == None),  # noqa: E711
        )

    (check,) = next(
        model
        for model in ensure_resolved_modelset()["payload"]["models"]
        if model["table_name"] == "coached"
    )["table_checks"]
    body = ferro._core._render_table_check_body(json.dumps(check["predicate"]))

    assert check["name"] == "ck_coached_no_coach"
    assert body == '"coach_player_id" IS NULL'
