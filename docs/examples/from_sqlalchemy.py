"""Runnable companion to the SQLAlchemy migration how-to (docs/pages/howto/migrate-from-sqlalchemy.md)."""

import asyncio

from ferro import ModelDoesNotExist, connect, engines, transaction

# --8<-- [start:models]
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation


class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True)
    email: str
    age: int
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    author: Annotated[User, ForeignKey(related_name="posts")]


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    # --8<-- [start:session]
    async with engines.session():
        ada = await User.create(username="ada", email="ada@example.com", age=36)
        assert ada.id is not None  # inserted now; the key is set
        # --8<-- [end:session]

        await User.create(username="grace", email="grace@example.com", age=45)
        await User.create(username="linus", email="linus@example.com", age=17)
        await User.create(username="alan", email="alan@example.com", age=41)

        # --8<-- [start:fetch-all]
        users = await User.all()
        assert [user.username for user in users] == ["ada", "grace", "linus", "alan"]
        # --8<-- [end:fetch-all]

        # --8<-- [start:filter]
        adults = (
            await User.where(lambda user: user.age >= 18)
            .order_by(lambda user: user.age)
            .limit(2)
            .all()
        )
        assert [user.username for user in adults] == ["ada", "alan"]
        # --8<-- [end:filter]

        # --8<-- [start:get]
        grace = await User.get(2)
        assert grace.username == "grace"

        try:
            await User.get(99)
        except ModelDoesNotExist:
            missing = None
        else:
            raise AssertionError("User.get(99) should raise ModelDoesNotExist")
        assert missing is None

        assert await User.get_or_none(99) is None
        # --8<-- [end:get]

        # --8<-- [start:upserts]
        user, created = await User.get_or_create(
            username="ada", defaults={"email": "other@example.com", "age": 1}
        )
        assert (created, user.id) == (False, ada.id)
        # --8<-- [end:upserts]

        # --8<-- [start:relationships]
        post = await Post.create(title="Notes on the engine", author=ada)
        await Post.create(title="Poetical science", author=ada)

        author = await post.author
        assert author.username == "ada"

        recent = await ada.posts.order_by(lambda post: post.id, "desc").limit(5).all()
        assert [row.title for row in recent] == [
            "Poetical science",
            "Notes on the engine",
        ]
        # --8<-- [end:relationships]

        # --8<-- [start:transactions]
        try:
            async with transaction():
                await User.create(username="margaret", email="m@example.com", age=30)
                raise RuntimeError("abort")
        except RuntimeError:
            ...  # the block raised, so the insert rolled back

        assert await User.where(lambda user: user.username == "margaret").count() == 0
        # --8<-- [end:transactions]


if __name__ == "__main__":
    asyncio.run(main())
