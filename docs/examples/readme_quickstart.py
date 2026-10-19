"""The README quick start (README.md), runnable.

tests/test_docs_examples.py checks that the README's Python block is this
file's code, marker comments aside, and runs it.
"""

import asyncio

# --8<-- [start:models]
from ferro import Field, Model, connect, engines


class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    username: str
    is_active: bool = True


# --8<-- [end:models]


async def main():
    # --8<-- [start:query]
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await User.create(username="alice")
        await User.create(username="bob", is_active=False)

        active_users = await User.where(lambda user: user.is_active == True).all()  # noqa: E712
        print(f"Found {len(active_users)} active users.")
        # --8<-- [end:query]


if __name__ == "__main__":
    asyncio.run(main())
