"""Runnable companion to the Mutations guide (docs/pages/guide/mutations.md)."""

import asyncio

from ferro import ModelDoesNotExist, UniqueViolationError, connect, engines, now

# --8<-- [start:models]
from datetime import datetime
from typing import Annotated

from ferro import Field, Model


class Customer(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    email: Annotated[str, Field(unique=True)]
    name: str = ""
    plan: str = "free"
    views: int = 0
    updated_at: datetime | None = None


# --8<-- [end:models]


# --8<-- [start:conversation]
class Conversation(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    turns: dict = Field(default_factory=dict)


# --8<-- [end:conversation]


async def main() -> None:
    # --8<-- [start:session]
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        alice = await Customer.create(email="alice@example.com", name="Alice")
        assert (alice.id, alice.plan) == (1, "free")
        # --8<-- [end:session]

        # --8<-- [start:create]
        bob = await Customer.create(email="bob@example.com", name="Bob", plan="pro")

        inserted = await Customer.bulk_create(
            [
                Customer(email="carol@example.com", name="Carol"),
                Customer(email="dave@example.com", name="Dave", plan="pro"),
            ]
        )
        assert inserted == 2
        assert await Customer.select().count() == 4
        # --8<-- [end:create]

        # --8<-- [start:bulk-identity]
        erin = Customer(email="erin@example.com", name="Erin")
        await Customer.bulk_create([erin])
        assert erin.id is None  # not written back

        erin = await Customer.where(
            lambda customer: customer.email == "erin@example.com"
        ).first()
        assert erin.id == 5
        # --8<-- [end:bulk-identity]

        # --8<-- [start:create-strict]
        try:
            await Customer.create(email="alice@example.com", name="Alice again")
        except UniqueViolationError:
            ...  # the existing row is untouched
        else:
            raise AssertionError("expected UniqueViolationError")
        assert (await Customer.get(alice.id)).name == "Alice"
        # --8<-- [end:create-strict]

        # --8<-- [start:save-insert-update]
        frank = Customer(email="frank@example.com", name="Frank")
        await frank.save()  # never persisted: INSERT
        assert frank.id is not None  # the database assigned it

        frank.plan = "pro"
        await frank.save()  # persisted: UPDATE by primary key

        franks = await Customer.where(lambda customer: customer.name == "Frank").count()
        assert franks == 1  # two saves, one row
        # --8<-- [end:save-insert-update]

        # --8<-- [start:save-missing]
        await Customer.where(lambda customer: customer.id == frank.id).delete()
        frank.name = "Franklin"
        try:
            await frank.save()
        except ModelDoesNotExist:
            ...  # the row is gone; save() does not insert it again
        else:
            raise AssertionError("expected ModelDoesNotExist")
        # --8<-- [end:save-missing]

        # --8<-- [start:save-only]
        row = await Customer.get(bob.id)
        row.name = "Robert"
        row.updated_at = datetime(2026, 3, 1, 9, 30)
        row.plan = "enterprise"  # not in only=, so not written
        await row.save(only={"name", "updated_at"})
        assert row.plan == "enterprise"  # the instance keeps its change

        await row.refresh()
        assert (row.name, row.plan) == ("Robert", "pro")  # the row kept "pro"
        # --8<-- [end:save-only]

        # --8<-- [start:save-exclude]
        row.name = "Bobby"
        row.plan = "enterprise"  # excluded, so not written
        await row.save(exclude={"plan"})

        await row.refresh()
        assert (row.name, row.plan) == ("Bobby", "pro")
        # --8<-- [end:save-exclude]

        # --8<-- [start:update-delete]
        upgraded = await Customer.where(
            lambda customer: customer.plan == "free"
        ).update(plan="trial")
        assert upgraded == 3  # Alice, Carol and Erin

        removed = await Customer.where(
            lambda customer: customer.name == "Erin"
        ).delete()
        assert removed == 1
        # --8<-- [end:update-delete]

        # --8<-- [start:recipe-update]
        await Customer.where(lambda customer: customer.id == alice.id).update(
            lambda customer: {
                "views": customer.views + 1,
                "updated_at": now,
            }
        )

        await alice.refresh()
        assert alice.views == 1 and alice.updated_at is not None
        # --8<-- [end:recipe-update]

        # .merge() exists on PostgreSQL only; the rendered statement says
        # which database this session runs on.
        on_postgres = (await Customer.select().count.sql()).dialect == "postgres"
        if on_postgres:
            # --8<-- [start:merge]
            chat = await Conversation.create()
            await Conversation.where(
                lambda conversation: conversation.id == chat.id
            ).update(
                lambda conversation: {
                    "turns": conversation.turns.merge({"r1": {"role": "user"}})
                }
            )

            await chat.refresh()
            assert chat.turns == {"r1": {"role": "user"}}
            # --8<-- [end:merge]

        # --8<-- [start:mutation-guard]
        trial = Customer.where(lambda customer: customer.plan == "trial")
        try:
            await trial.limit(1).delete()
        except ValueError as error:
            print(error)
        else:
            raise AssertionError("expected ValueError")
        # --8<-- [end:mutation-guard]

        # --8<-- [start:mutation-by-key]
        oldest = await trial.order_by(lambda customer: customer.id).limit(1).all()
        ids = [customer.id for customer in oldest]
        removed = await Customer.where(lambda customer: customer.id.in_(ids)).delete()
        assert removed == 1  # Alice, the oldest trial customer
        # --8<-- [end:mutation-by-key]

        # --8<-- [start:delete-one]
        carol = await Customer.where(
            lambda customer: customer.email == "carol@example.com"
        ).first()
        await carol.delete()

        assert await Customer.get_or_none(carol.id) is None
        # --8<-- [end:delete-one]

        # --8<-- [start:upsert]
        grace = await Customer.upsert(id=100, email="grace@example.com", name="Grace")

        grace = await Customer.upsert(id=100, email="grace@example.com", plan="pro")
        assert grace.plan == "pro"
        assert await Customer.where(lambda customer: customer.id == 100).count() == 1

        stored = await Customer.get(100)
        assert (stored.name, stored.plan) == ("", "pro")  # name reset to its default
        # --8<-- [end:upsert]

        # --8<-- [start:get-or-create]
        heidi, created = await Customer.get_or_create(
            email="heidi@example.com",
            defaults={"name": "Heidi"},
        )
        assert created is True

        same, created = await Customer.get_or_create(email="heidi@example.com")
        assert created is False and same.id == heidi.id
        # --8<-- [end:get-or-create]

        # --8<-- [start:update-or-create]
        heidi, created = await Customer.update_or_create(
            email="heidi@example.com",
            defaults={"plan": "pro"},
        )
        assert created is False and heidi.plan == "pro"
        # --8<-- [end:update-or-create]

        # --8<-- [start:refresh]
        await Customer.where(lambda customer: customer.id == heidi.id).update(
            name="Heidi L."
        )
        assert heidi.name == "Heidi"  # the instance has not seen the update

        await heidi.refresh()
        assert heidi.name == "Heidi L."
        # --8<-- [end:refresh]

        # --8<-- [start:handling-errors]
        try:
            await Customer.create(email="heidi@example.com")  # email already taken
        except UniqueViolationError as error:
            print(error.sqlstate)
            print(error.constraint)
            print(error.driver_message)
        else:
            raise AssertionError("expected UniqueViolationError")
        # --8<-- [end:handling-errors]

    print("mutations example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
