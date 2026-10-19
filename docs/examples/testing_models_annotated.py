"""Annotated-style companion to testing_models.py (AGENTS.md I-7)."""

import asyncio

# --8<-- [start:models]
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation


class User(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    username: Annotated[str, Field(unique=True)]
    email: Annotated[str, Field(unique=True)]
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    title: str
    content: str
    author: Annotated[User, ForeignKey(related_name="posts")]


# --8<-- [end:models]


async def main() -> None:
    from ferro import connect, engines

    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        ada = await User.create(username="ada", email="ada@example.com")
        post = await Post.create(title="Notes", content="On the engine", author=ada)
        assert (await post.author).username == "ada"

    print("testing_models_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
