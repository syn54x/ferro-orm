"""Assigning a forward relation sets the foreign-key column behind it.

```python
player.team = crabs      # team_id becomes crabs.id; player.team is crabs, no query
player.mentor = None     # mentor_id becomes None; player.mentor reads None
player.team_id = 9       # the loaded team is dropped; player.team reads Team(id=9, unloaded)
```

Each test declares its own models in an empty registry, so the names in the
refusals (`Player.team`, `team_id`) read as a user's would.
"""

from types import SimpleNamespace

import pytest

import ferro
from ferro import (
    Field,
    Forward,
    Model,
    Relation,
    Reverse,
    clear_registry,
    engines,
    reset_engine,
)
from ferro.registry import REGISTRY

pytestmark = pytest.mark.backend_matrix


@pytest.fixture(autouse=True)
def _empty_registry():
    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()
    yield
    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


def _declare() -> SimpleNamespace:
    """Team, Coach and Player (a required team and a nullable mentor)."""

    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        players: Relation[list["Player"]] = Reverse("team_id")

    class Coach(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        team_id: int = Field(foreign_key=Team)
        team: Relation[Team] = Forward("team_id")
        mentor_id: int | None = Field(
            default=None, foreign_key="Player", on_delete="SET NULL"
        )
        mentor: Relation["Player | None"] = Forward("mentor_id")

    return SimpleNamespace(Team=Team, Coach=Coach, Player=Player)


async def _seed(db_url: str) -> SimpleNamespace:
    """Teams "Rustaceans" (1) and "Crabs" (2), coach Pat (1), Ferris (player 1,
    team 1) and Corro (player 2, team 1, mentored by Ferris)."""
    m = _declare()
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        await m.Team.create(id=1, name="Rustaceans")
        await m.Team.create(id=2, name="Crabs")
        await m.Coach.create(id=1, name="Pat")
        await m.Player.create(id=1, name="Ferris", team_id=1)
        await m.Player.create(id=2, name="Corro", team_id=1, mentor_id=1)
    return m


@pytest.mark.asyncio
async def test_assigning_a_team_sets_its_key_and_loads_the_relation_without_a_query(
    db_url, statement_records
):
    m = await _seed(db_url)
    async with engines.session():
        player = await m.Player.get(1)
        crabs = await m.Team.get(2)
        statement_records.clear()

        player.team = crabs

        assert player.team_id == 2
        assert player.team is crabs
        assert statement_records == []
