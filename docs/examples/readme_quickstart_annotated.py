"""Annotated-style companion to readme_quickstart.py (AGENTS.md I-7)."""

import asyncio

# --8<-- [start:models]
from typing import Annotated

from ferro import Field, Model, connect, engines


class User(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
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
        assert [user.username for user in active_users] == ["alice"]
        print(f"Found {len(active_users)} active users.")
        # --8<-- [end:query]


if __name__ == "__main__":
    asyncio.run(main())
