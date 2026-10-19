"""Runnable companion to the Quickstart tutorial (docs/pages/getting-started/quickstart.md)."""

import asyncio

from ferro import connect, engines, transaction

# --8<-- [start:models]
from datetime import UTC, datetime
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    author: Annotated[Author, ForeignKey(related_name="posts")]


# --8<-- [end:models]


async def main() -> None:
    # --8<-- [start:connect]
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        assert await Author.select().count() == 0
        # --8<-- [end:connect]

        # --8<-- [start:create]
        alice = await Author.create(name="Alice", email="alice@example.com")
        post = await Post.create(
            title="Why Ferro is fast",
            body="Ferro hands SQL generation and row hydration to a Rust engine.",
            published=True,
            author=alice,
        )
        assert post.id == 1  # the database assigned it
        # --8<-- [end:create]

        # --8<-- [start:bulk-create]
        await Post.bulk_create(
            [
                Post(
                    title="Async patterns",
                    body="One event loop, many queries.",
                    published=True,
                    author=alice,
                ),
                Post(
                    title="Unfinished draft",
                    body="Still to write.",
                    author=alice,
                ),
            ]
        )
        assert await Post.select().count() == 3
        # --8<-- [end:bulk-create]

        # --8<-- [start:get]
        same_post = await Post.get(post.id)
        assert same_post is post  # one row, one object
        # --8<-- [end:get]

        # --8<-- [start:filter]
        published = (
            await Post.where(lambda post: post.published == True)  # noqa: E712
            .order_by(lambda post: post.title)
            .limit(10)
            .all()
        )
        assert [post.title for post in published] == [
            "Async patterns",
            "Why Ferro is fast",
        ]
        # --8<-- [end:filter]

        # --8<-- [start:aggregate]
        total = await Post.select().count()
        has_drafts = await Post.where(lambda post: post.published == False).exists()  # noqa: E712
        assert (total, has_drafts) == (3, True)
        # --8<-- [end:aggregate]

        # --8<-- [start:relationships]
        author = await post.author
        assert author.name == "Alice"

        alice_posts = await author.posts.where(
            lambda post: post.published == True  # noqa: E712
        ).all()
        assert len(alice_posts) == 2
        # --8<-- [end:relationships]

        # --8<-- [start:update-delete]
        post.title = "Why Ferro is really fast"
        await post.save()
        assert (await Post.get(post.id)).title == "Why Ferro is really fast"

        updated = await Post.where(lambda post: post.published == False).update(  # noqa: E712
            published=True
        )
        assert updated == 1

        deleted = await Post.where(
            lambda post: post.title == "Unfinished draft"
        ).delete()
        assert deleted == 1
        # --8<-- [end:update-delete]

        # --8<-- [start:transaction]
        async with transaction():
            bob = await Author.create(name="Bob", email="bob@example.com")
            await Post.create(title="Hello", body="First post.", author=bob)
        assert await Author.select().count() == 2  # both writes committed
        # --8<-- [end:transaction]

    print("quickstart example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
