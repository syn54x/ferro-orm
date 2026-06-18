"""Runnable companion to the Transactions guide (docs/pages/guide/transactions.md)."""

import asyncio

from ferro import Field, Model, connect, transaction


class Account(Model):
    id: int | None = Field(default=None, primary_key=True)
    owner: str
    balance: int = 0


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    checking = await Account.create(owner="alice", balance=100)
    savings = await Account.create(owner="alice", balance=0)

    # --8<-- [start:basic]
    async with transaction():
        checking.balance -= 25
        await checking.save()

        savings.balance += 25
        await savings.save()
    # Both writes commit together when the block exits cleanly
    # --8<-- [end:basic]
    await checking.refresh()
    assert checking.balance == 75

    # --8<-- [start:rollback]
    try:
        async with transaction():
            checking.balance -= 1000
            await checking.save()
            raise RuntimeError("insufficient funds")
    except RuntimeError:
        pass

    await checking.refresh()
    # The failed write was rolled back
    assert checking.balance == 75
    # --8<-- [end:rollback]

    # --8<-- [start:handle]
    async with transaction() as tx:
        await Account.create(owner="bob")
        # Raw SQL on the same connection, inside the same transaction
        rows = await tx.fetch_all("SELECT COUNT(*) AS n FROM account")
    # --8<-- [end:handle]
    assert rows[0]["n"] == 3

    print("transactions example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
