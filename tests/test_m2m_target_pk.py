"""#830: many-to-many add()/remove() identify instances by the primary key."""

from typing import Annotated
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import BackRef, Field, ManyToMany, Model, Relation
from ferro.raw import fetch_all
from tests._pass_harness import auto_migrate


@pytest.fixture
def slug_models():
    from ferro import clear_registry, reset_engine
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()

    class Tag(Model):
        slug: str | None = Field(default=None, primary_key=True)
        label: str = ""
        posts: Relation[list["Post"]] = ManyToMany(related_name="tags")

    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        tags: Relation[list[Tag]] = BackRef()

    class Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    return Tag, Post, Author


async def _links(table: str) -> int:
    rows = await fetch_all(f'SELECT count(*) AS n FROM "{table}"')
    return rows[0]["n"]


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_add_all_remove_with_slug_keyed_target(db_url, slug_models):
    Tag, Post, _ = slug_models
    await auto_migrate(db_url)
    async with ferro.engines.session():
        tag = await Tag.create(slug="rust", label="Rust")
        post = await Post.create(title="Hello")
        await post.tags.add(tag)

        rows = await fetch_all('SELECT "tag_id" FROM "tag_posts"')
        assert [r["tag_id"] for r in rows] == ["rust"]
        assert [t.slug for t in await post.tags.all()] == ["rust"]

        await post.tags.remove(tag)
        assert await _links("tag_posts") == 0
        assert await post.tags.count() == 0


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_owner_side_slug_keyed_still_works(db_url, slug_models):
    Tag, Post, _ = slug_models
    await auto_migrate(db_url)
    async with ferro.engines.session():
        tag = await Tag.create(slug="rust", label="Rust")
        post = await Post.create(title="Hello")
        await tag.posts.add(post)
        assert [p.id for p in await tag.posts.all()] == [post.id]
        await tag.posts.remove(post)
        assert await tag.posts.count() == 0


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_uuid_keyed_target(db_url):
    from ferro import clear_registry, reset_engine
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()

    class Badge(Model):
        uid: Annotated[UUID, Field(primary_key=True)] = Field(
            default_factory=uuid4, primary_key=True
        )
        label: str = ""
        owners: Relation[list["Member"]] = ManyToMany(related_name="badges")

    class Member(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        badges: Relation[list[Badge]] = BackRef()

    await auto_migrate(db_url)
    async with ferro.engines.session():
        badge = await Badge.create(label="gold")
        member = await Member.create(name="Ada")
        await member.badges.add(badge)
        assert [b.uid for b in await member.badges.all()] == [badge.uid]
        await member.badges.remove(badge)
        assert await member.badges.count() == 0


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize("verb", ["add", "remove"])
async def test_unsaved_instance_is_refused(db_url, slug_models, verb):
    Tag, Post, _ = slug_models
    await auto_migrate(db_url)
    async with ferro.engines.session():
        post = await Post.create(title="Hello")
        with pytest.raises(ValueError) as exc:
            await getattr(post.tags, verb)(Tag(label="x"))
        assert str(exc.value) == (
            f"post.tags.{verb}(): Tag(slug=None) has no primary key yet; save it first"
        )
        assert await _links("tag_posts") == 0


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize("verb", ["add", "remove"])
async def test_foreign_model_instance_is_refused(db_url, slug_models, verb):
    Tag, Post, Author = slug_models
    await auto_migrate(db_url)
    async with ferro.engines.session():
        post = await Post.create(title="Hello")
        author = await Author.create(name="Ada")
        with pytest.raises(TypeError) as exc:
            await getattr(post.tags, verb)(author)
        assert str(exc.value) == (f"post.tags.{verb}() takes Tag instances, got Author")
        assert await _links("tag_posts") == 0
