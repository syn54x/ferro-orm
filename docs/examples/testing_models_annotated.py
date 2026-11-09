"""The models of the testing how-to (docs/pages/howto/testing.md).

The page's readers keep these in ``myapp/models.py``;
``tests/test_docs_testing_fixtures.py`` copies this file there and runs the
page's tests against it.
"""

import asyncio
from typing import Annotated

# --8<-- [start:models]
from ferro import Field, Forward, Model, Relation, Reverse


class User(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    username: Annotated[str, Field(unique=True)]
    email: Annotated[str, Field(unique=True)]
    posts: Relation[list["Post"]] = Reverse("author_id")


class Post(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    title: str
    content: str
    author_id: Annotated[int, Field(foreign_key=User)]
    author: Relation[User] = Forward("author_id")


# --8<-- [end:models]


async def main() -> None:
    from ferro import connect, engines

    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        ada = await User.create(username="ada", email="ada@example.com")
        post = await Post.create(
            title="Notes", content="On the engine", author_id=ada.id
        )
        assert (await post.author.load()).username == "ada"

    print("testing_models example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
