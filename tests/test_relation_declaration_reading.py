"""A relation written in a class body is read into a record, or refused.

Every test defines model classes and reads the refusal the class raises or
the record it publishes.
"""

import pytest

from ferro import Field, Forward, Model, Relation, Reverse
from ferro.base import ForwardRelation
from ferro.relations.fixes import DeclarationRefusal

pytestmark = pytest.mark.usefixtures("clean_registry")


def test_a_fault_an_earlier_step_catches_is_reported_before_the_relation():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(TypeError, match="db_check=True requires db_type"):

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            team: Team = Forward("team_id")
            nickname: str = Field(db_check=True)


def test_a_refused_class_leaves_room_for_the_fixed_one():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(DeclarationRefusal):

        class Player(Model):
            id: int | None = Field(default=None, primary_key=True)
            team_id: int = Field(foreign_key=Team)
            team: Team = Forward("team_id")

    class Player(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key=Team)
        team: Relation[Team] = Forward("team_id")

    assert "team" in Player.__ferro_relations__


def test_a_reverse_not_annotated_relation_is_refused_once_the_class_exists():
    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key="Team")

    with pytest.raises(DeclarationRefusal) as refused:

        class Team(Model):
            id: int | None = Field(default=None, primary_key=True)
            players: list[Player] = Reverse("team")

    assert "Relation[list[Player]]" in str(refused.value)


def test_a_record_shows_no_refusal_or_bound():
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        team_id: int = Field(foreign_key=Team)
        team: Relation[Team] = Forward("team_id")

    record = Player.__ferro_relations__["team"]
    assert not hasattr(record, "refusal")
    assert not hasattr(record, "bound")


def test_a_record_takes_no_refusal_keyword():
    with pytest.raises(TypeError):
        ForwardRelation("team_id", None, False, refusal="x")
