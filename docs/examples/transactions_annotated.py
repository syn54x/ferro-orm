"""Runnable companion to the Transactions guide (docs/pages/guide/transactions.md)."""

import asyncio
from typing import Annotated

from ferro import IntegrityError, connect, engines, transaction

# --8<-- [start:models]
from ferro import Field, Model


class Account(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    number: Annotated[str, Field(unique=True)]
    owner: str
    balance: int = 0


# --8<-- [end:models]


async def quote_interest(account: Account) -> int:
    """Stand-in for a slow call made outside the transaction."""
    await asyncio.sleep(0)
    return account.balance // 50


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    # --8<-- [start:basic]
    async with engines.session():
        checking = await Account.create(number="CHK-001", owner="Alice", balance=100)
        savings = await Account.create(number="SAV-001", owner="Alice", balance=0)

        async with transaction():
            checking.balance -= 25
            await checking.save()

            savings.balance += 25
            await savings.save()

        assert (checking.balance, savings.balance) == (75, 25)
        # --8<-- [end:basic]

        # --8<-- [start:rollback]
        try:
            async with transaction():
                checking.balance -= 1000
                await checking.save()
                raise RuntimeError("insufficient funds")
        except RuntimeError:
            ...  # the block raised, so everything in it rolled back

        assert checking.balance == -925  # the Python object kept the change

        await checking.refresh()
        assert checking.balance == 75  # the row did not
        # --8<-- [end:rollback]

        # --8<-- [start:transfer]
        async def transfer(src: Account, dst: Account, amount: int) -> bool:
            try:
                async with transaction():
                    src.balance -= amount
                    await src.save()
                    if src.balance < 0:
                        raise ValueError("insufficient funds")
                    dst.balance += amount
                    await dst.save()
            except ValueError:
                await src.refresh()  # undo the in-memory change too
                return False
            return True

        assert await transfer(checking, savings, 1000) is False
        assert (checking.balance, savings.balance) == (75, 25)

        assert await transfer(checking, savings, 50) is True
        assert (checking.balance, savings.balance) == (25, 75)
        # --8<-- [end:transfer]

        # --8<-- [start:nested]
        opened = []
        async with transaction():
            for number in ["CHK-002", "CHK-001", "SAV-002"]:
                try:
                    async with transaction():  # a savepoint for each account
                        await Account.create(number=number, owner="Bob")
                        opened.append(number)
                except IntegrityError:
                    ...  # CHK-001 is taken: only this savepoint rolled back

        assert opened == ["CHK-002", "SAV-002"]
        assert await Account.where(lambda account: account.owner == "Bob").count() == 2
        # --8<-- [end:nested]

        # --8<-- [start:handle]
        async with transaction() as tx:
            await Account.create(number="CHK-003", owner="Carol")
            rows = await tx.fetch_all("SELECT COUNT(*) AS n FROM account")

        assert rows[0]["n"] == 5  # the count saw Carol's uncommitted row
        # --8<-- [end:handle]

        # --8<-- [start:immediate]
        async with transaction(immediate=True):
            savings = await Account.get(savings.id)  # read, then write
            savings.balance += 10
            await savings.save()

        assert savings.balance == 85
        # --8<-- [end:immediate]

        # --8<-- [start:short]
        interest = await quote_interest(savings)  # slow work first

        async with transaction():  # then a short write
            savings.balance += interest
            await savings.save()

        assert savings.balance == 86
        # --8<-- [end:short]

    print("transactions example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
