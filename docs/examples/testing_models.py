"""The models of the testing how-to (docs/pages/howto/testing.md).

The page's readers keep these in ``myapp/models.py``;
``tests/test_docs_testing_fixtures.py`` copies this file there and runs the
page's tests against it.
"""

import asyncio

# --8<-- [start:models]
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation


class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True)
    email: str = Field(unique=True)
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
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

    print("testing_models example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
