"""The constructor takes a forward relation's foreign-key column, never the relation.

```python
player = await Player.create(handle="ferris", team_id=rustaceans.id)   # the one spelling
Player(handle="ferris", team=rustaceans)
# TypeError: Player.team is a relation. Pass the foreign-key column's value,
# team_id=team.id, or set player.team = team on the instance
```

A reverse or many-to-many relation keyword is refused naming its fix, an
unknown keyword is Pydantic's `extra_forbidden`, and `where().update()` names
the foreign-key column too. Each test declares its own models in an
empty registry, so the names in the refusals read as a user's would.
"""

from types import SimpleNamespace

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
    engines,
    reset_engine,
)
from ferro.registry import REGISTRY

pytestmark = pytest.mark.backend_matrix

# `team_id` is required, so the refusal names the one fix a constructor can
# take; a nullable key also names the assignment (I-6).
REFUSAL = (
    "Player.team is a relation. Pass the foreign-key column's value, team_id=team.id"
)
UPDATE_REFUSAL = (
    "update() cannot write Player.team, a relation. Name its foreign-key "
    "column, team_id, instead."
)


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
    class Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        players: Relation[list["Player"]] = Reverse("team_id")

    class Player(Model):
        id: int | None = Field(default=None, primary_key=True)
        handle: str
        team_id: int = Field(foreign_key=Team)
        team: Relation[Team] = Forward("team_id")
        mentor_id: int | None = Field(
            default=None, foreign_key="Player", on_delete="SET NULL"
        )
        mentor: Relation["Player | None"] = Forward("mentor_id")

    return SimpleNamespace(Team=Team, Player=Player)


async def _seed(db_url: str) -> SimpleNamespace:
    """Teams "Rustaceans" (1) and "Crabs" (2), and Ferris (player 1, team 1)."""
    m = _declare()
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        m.rustaceans = await m.Team.create(name="Rustaceans")
        m.crabs = await m.Team.create(name="Crabs")
        m.ferris = await m.Player.create(handle="ferris", team_id=m.rustaceans.id)
    return m


@pytest.mark.asyncio
async def test_create_saves_the_foreign_key_column(db_url):
    m = await _seed(db_url)
    async with engines.session():
        row = await m.Player.get(m.ferris.id)
        assert row.team_id == m.rustaceans.id


def test_a_relation_keyword_on_a_required_key_names_the_key_column():
    m = _declare()
    team = m.Team(id=1, name="Rustaceans")

    with pytest.raises(TypeError) as refused:
        m.Player(handle="ferris", team=team)

    assert str(refused.value) == REFUSAL


def test_the_relation_no_longer_wins_over_the_key():
    m = _declare()
    team, other = m.Team(id=1, name="Rustaceans"), m.Team(id=2, name="Crabs")

    with pytest.raises(TypeError) as refused:
        m.Player(handle="ferris", team_id=team.id, team=other)

    assert str(refused.value) == REFUSAL


@pytest.mark.asyncio
async def test_the_fix_named_for_a_required_key_builds_the_player(db_url):
    """I-6: the call rewritten as the refusal says saves the player's team."""
    m = await _seed(db_url)
    team = m.crabs
    async with engines.session():
        player = await m.Player.create(handle="corro", team_id=team.id)

        assert (await m.Player.get(player.id)).team_id == team.id


def test_a_nullable_key_names_both_fixes_and_each_builds_the_player():
    """I-6: a key the constructor can leave out also offers the assignment."""
    m = _declare()
    mentor = m.Player(id=7, handle="ferris", team_id=1)

    with pytest.raises(TypeError) as refused:
        m.Player(handle="corro", team_id=1, mentor=mentor)

    assert str(refused.value) == (
        "Player.mentor is a relation. Pass the foreign-key column's value, "
        "mentor_id=mentor.id, or set player.mentor = mentor on the instance"
    )
    assert m.Player(handle="corro", team_id=1, mentor_id=mentor.id).mentor_id == 7
    player = m.Player(handle="corro", team_id=1)
    player.mentor = mentor
    assert player.mentor_id == 7


@pytest.mark.asyncio
async def test_every_constructing_class_method_refuses_the_relation(db_url):
    m = await _seed(db_url)
    team = m.rustaceans
    async with engines.session():
        for build in (
            lambda: m.Player.create(handle="corro", team=team),
            lambda: m.Player.upsert(handle="corro", team=team),
            lambda: m.Player.using("default").create(handle="corro", team=team),
            lambda: m.Player.using("default").upsert(handle="corro", team=team),
        ):
            with pytest.raises(TypeError) as refused:
                await build()
            assert str(refused.value) == REFUSAL
        assert await m.Player.select().count() == 1


def test_model_validate_refuses_the_relation():
    m = _declare()
    team = m.Team(id=1, name="Rustaceans")

    with pytest.raises(TypeError) as refused:
        m.Player.model_validate({"handle": "ferris", "team": team})

    assert str(refused.value) == REFUSAL


@pytest.mark.asyncio
async def test_get_or_create_refuses_before_any_statement_even_when_a_row_matches(
    db_url, statement_records
):
    m = await _seed(db_url)
    team = m.rustaceans
    async with engines.session():
        statement_records.clear()
        for lookup in (
            lambda: m.Player.get_or_create(team=team, handle="ferris"),
            lambda: m.Player.get_or_create(handle="ferris", defaults={"team": team}),
            lambda: m.Player.using("default").get_or_create(team=team, handle="ferris"),
        ):
            with pytest.raises(TypeError) as refused:
                await lookup()
            assert str(refused.value) == REFUSAL
        assert statement_records == []


@pytest.mark.asyncio
async def test_update_or_create_refuses_before_any_statement(db_url, statement_records):
    m = await _seed(db_url)
    team = m.crabs
    async with engines.session():
        statement_records.clear()
        for lookup in (
            lambda: m.Player.update_or_create(handle="ferris", defaults={"team": team}),
            lambda: m.Player.update_or_create(team=team, handle="ferris"),
            lambda: m.Player.using("default").update_or_create(
                handle="ferris", defaults={"team": team}
            ),
        ):
            with pytest.raises(TypeError) as refused:
                await lookup()
            assert str(refused.value) == REFUSAL
        assert statement_records == []

        # The fix: the key column in `defaults`.
        player, created = await m.Player.update_or_create(
            handle="ferris", defaults={"team_id": team.id}
        )
        assert created is False
        assert (await m.Player.get(player.id)).team_id == team.id


def test_the_refusal_names_the_target_primary_key_and_a_snake_case_instance():
    class Order(Model):
        code: str = Field(primary_key=True)

    class LineItem(Model):
        id: int | None = Field(default=None, primary_key=True)
        order_id: str | None = Field(default=None, foreign_key=Order)
        order: Relation[Order | None] = Forward("order_id")

    order = Order(code="A-1")
    with pytest.raises(TypeError) as refused:
        LineItem(order=order)

    assert str(refused.value) == (
        "LineItem.order is a relation. Pass the foreign-key column's value, "
        "order_id=order.code, or set line_item.order = order on the instance"
    )
    # Both fixes build the item.
    assert LineItem(order_id=order.code).order_id == "A-1"
    item = LineItem()
    item.order = order
    assert item.order_id == "A-1"


def test_an_unknown_keyword_is_refused():
    m = _declare()

    with pytest.raises(ValidationError) as refused:
        m.Player(handle="x", team_id=1, teamid=1)

    errors = refused.value.errors()
    assert [(error["type"], error["loc"]) for error in errors] == [
        ("extra_forbidden", ("teamid",))
    ]


def test_a_reverse_relation_keyword_names_the_child_key_column():
    m = _declare()

    with pytest.raises(TypeError) as refused:
        m.Team(name="crabs", players=[])

    assert str(refused.value) == (
        "Team.players is a relation. Save the team, then set "
        "player.team_id = team.id for each player in players"
    )


@pytest.mark.asyncio
async def test_a_many_to_many_keyword_names_add_and_its_fix_links_the_rows(db_url):
    class Tag(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str

    class Article(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        tags: Relation[list[Tag]] = ManyToMany(through="article_tags")

    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        tags = [await Tag.create(label="history")]
        for build in (
            lambda: Article.create(title="Babbage", tags=tags),
            lambda: Article.get_or_create(title="Babbage", tags=tags),
        ):
            with pytest.raises(TypeError) as refused:
                await build()
            assert str(refused.value) == (
                "Article.tags is a relation. Save the article, then "
                "await article.tags.add(*tags)"
            )

        article = await Article.create(title="Babbage")
        await article.tags.add(*tags)
        assert [tag.label for tag in await article.tags.all()] == ["history"]


@pytest.mark.asyncio
async def test_model_forbids_extra_keys_and_hydration_is_unaffected(db_url):
    m = await _seed(db_url)

    config = m.Player.model_config
    assert config["extra"] == "forbid"
    assert config["from_attributes"] is True
    assert config["use_attribute_docstrings"] is True
    assert config["arbitrary_types_allowed"] is True
    async with engines.session():
        row = await m.Player.get(m.ferris.id)
        assert row.__pydantic_extra__ is None


@pytest.mark.asyncio
async def test_update_refuses_a_relation_keyword_naming_its_key_column(db_url):
    m = await _seed(db_url)
    other = m.crabs
    async with engines.session():
        for update in (
            lambda: m.Player.where(lambda player: player.id == 1).update(team=other),
            lambda: m.Player.where(lambda player: player.id == 1).update(
                lambda player: {"team": 2}
            ),
        ):
            with pytest.raises(TypeError) as refused:
                await update()
            assert str(refused.value) == UPDATE_REFUSAL
            assert "Did you mean 'team'" not in str(refused.value)

        # The fix: name the foreign-key column.
        updated = await m.Player.where(lambda player: player.id == 1).update(
            team_id=other.id
        )
        assert updated == 1
        assert (await m.Player.get(1)).team_id == other.id


def test_a_query_naming_a_relation_as_a_column_names_its_key_column():
    m = _declare()

    with pytest.raises(AttributeError) as refused:
        m.Player.select().order_by("team")

    assert str(refused.value) == (
        "Player has no queryable column 'team': it is a relation. Its "
        "foreign-key column is 'team_id'."
    )
    # The fix: order by the column it names.
    m.Player.select().order_by("team_id")


@pytest.mark.asyncio
async def test_a_misspelled_update_column_keeps_its_closest_match(db_url):
    m = await _seed(db_url)
    async with engines.session():
        with pytest.raises(AttributeError, match=r"Did you mean 'team_id'\?"):
            await m.Player.where(lambda player: player.id == 1).update(tem_id=1)
