"""Runnable companion to the Quickstart tutorial (docs/pages/getting-started/quickstart.md)."""

import asyncio

# --8<-- [start:models]
from datetime import datetime
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, transaction


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    body: str
    published: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    author: Annotated[Author, ForeignKey(related_name="posts")]
# --8<-- [end:models]


async def main() -> None:
    # --8<-- [start:connect]
    await connect("sqlite::memory:", auto_migrate=True)
    # --8<-- [end:connect]

    # --8<-- [start:create]
    alice = await Author.create(name="Alice", email="alice@example.com")

    post = await Post.create(
        title="Why Ferro is Fast",
        body="Ferro hands SQL generation and row hydration to a Rust engine...",
        published=True,
        author=alice,
    )

    # Insert many rows in a single statement
    await Post.bulk_create(
        [
            Post(title="Async Patterns", body="...", published=True, author_id=alice.id),
            Post(title="Unfinished Draft", body="...", author_id=alice.id),
        ]
    )
    # --8<-- [end:create]
    assert post.id is not None

    # --8<-- [start:query]
    # Fetch by primary key
    same_post = await Post.get(post.id)

    # Filter, order, and slice
    published = (
        await Post.where(lambda t: t.published == True)  # noqa: E712
        .order_by(Post.created_at, "desc")
        .limit(10)
        .all()
    )

    # Aggregate terminals
    total = await Post.select().count()
    has_drafts = await Post.where(lambda t: t.published == False).exists()  # noqa: E712
    # --8<-- [end:query]
    assert same_post is post  # identity map: same Python object
    assert len(published) == 2
    assert total == 3
    assert has_drafts

    # --8<-- [start:relationships]
    # Forward access: awaiting the foreign key loads the related instance
    author = await same_post.author

    # Reverse access: the BackRef is a chainable query
    alice_posts = await author.posts.where(lambda t: t.published == True).all()  # noqa: E712
    # --8<-- [end:relationships]
    assert author.name == "Alice"
    assert len(alice_posts) == 2

    # --8<-- [start:update-delete]
    # Update one instance
    post.title = "Why Ferro is *Really* Fast"
    await post.save()

    # Update many rows at once
    updated = await Post.where(lambda t: t.published == False).update(published=True)  # noqa: E712

    # Delete
    deleted = await Post.where(lambda t: t.title == "Unfinished Draft").delete()
    # --8<-- [end:update-delete]
    assert updated == 1
    assert deleted == 1

    # --8<-- [start:transaction]
    async with transaction():
        bob = await Author.create(name="Bob", email="bob@example.com")
        await Post.create(title="Hello", body="...", author=bob)
    # Commits on success, rolls back if the block raises
    # --8<-- [end:transaction]
    assert await Author.select().count() == 2

    print("quickstart example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
