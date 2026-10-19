"""The running example of the Upgrade guide (docs/pages/howto/upgrade-guide.md).

One ``Account`` model, declared in the assignment style, and the session a
current release runs its queries in. The Annotated twin is
``upgrade_account_annotated.py``.
"""

import asyncio
from datetime import UTC, datetime
from enum import StrEnum

from ferro import Field, Model, connect, engines


# --8<-- [start:models]
class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class Account(Model):
    # --8<-- [start:primary-key]
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True)
    # --8<-- [end:primary-key]
    name: str
    role: Role = Role.MEMBER
    age: int
    active: bool = True
    seen_at: datetime | None = None
    prefs: dict = Field(default_factory=dict)


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    # --8<-- [start:session]
    async with engines.session():
        await Account.create(
            name="Ada Lovelace",
            email="ada@example.com",
            role=Role.ADMIN,
            age=36,
            seen_at=datetime(2026, 3, 1, 15, 0, tzinfo=UTC),
        )
        await Account.create(name="Grace Hopper", email="grace@example.com", age=45)
        adults = await Account.where(lambda account: account.age >= 40).all()
        assert [account.name for account in adults] == ["Grace Hopper"]
    # --8<-- [end:session]


if __name__ == "__main__":
    asyncio.run(main())
