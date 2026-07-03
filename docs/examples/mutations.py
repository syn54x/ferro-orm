"""Runnable companion to the Mutations guide (docs/pages/guide/mutations.md)."""

import asyncio

from ferro import Field, Model, UniqueViolationError, connect, engines


class Customer(Model):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True)
    name: str = ""
    plan: str = "free"


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        # --8<-- [start:get-or-create]
        customer, created = await Customer.get_or_create(
            email="alice@example.com",
            defaults={"name": "Alice"},
        )
        assert created is True

        # Second call finds the existing row instead of inserting
        same, created = await Customer.get_or_create(email="alice@example.com")
        assert created is False and same.id == customer.id
        # --8<-- [end:get-or-create]

        # --8<-- [start:update-or-create]
        customer, created = await Customer.update_or_create(
            email="alice@example.com",
            defaults={"plan": "pro"},
        )
        assert created is False and customer.plan == "pro"
        # --8<-- [end:update-or-create]

        # --8<-- [start:refresh]
        # Reload an instance from the database, discarding local state
        await Customer.where(lambda customer: customer.email == "alice@example.com").update(
            name="Alice L."
        )
        await customer.refresh()
        assert customer.name == "Alice L."
        # --8<-- [end:refresh]

        # --8<-- [start:create-strict]
        alice = await Customer.get(customer.id)

        # create() never updates an existing row — a duplicate is an error
        try:
            await Customer.create(id=alice.id, email="alice.2@example.com")
        except UniqueViolationError:
            ...  # the existing row is untouched
        # --8<-- [end:create-strict]

        # --8<-- [start:save-insert-update]
        bob = Customer(email="bob@example.com", name="Bob")
        await bob.save()  # never persisted: INSERT (bob.id is now set)

        bob.plan = "pro"
        await bob.save()  # persisted: UPDATE ... WHERE id = ?

        bobs = await Customer.where(lambda customer: customer.name == "Bob").count()
        assert bobs == 1  # two saves, one row
        # --8<-- [end:save-insert-update]

        # --8<-- [start:upsert]
        # No row with this primary key: INSERT
        carol = await Customer.upsert(id=100, email="carol@example.com", name="Carol")

        # Same primary key again: UPDATE of the existing row
        carol = await Customer.upsert(id=100, email="carol@example.com", plan="pro")
        assert carol.plan == "pro"
        assert await Customer.where(lambda customer: customer.id == 100).count() == 1

        # The whole row is written: `name` was left unset above, so the stored
        # "Carol" reverted to the field default — pass every field you care about.
        stored = await Customer.get(100)
        assert stored.name == ""
        # --8<-- [end:upsert]

        # --8<-- [start:handling-errors]
        try:
            await Customer.create(email="alice@example.com")  # email already taken
        except UniqueViolationError as exc:
            print(exc.sqlstate)  # "23505" (SQLSTATE) on Postgres, "2067" on SQLite
            print(exc.constraint)  # violated constraint name (Postgres only)
            print(exc.driver_message)  # original driver text, for logs
        # --8<-- [end:handling-errors]

        await Customer.create(email="dora@example.com")  # plan defaults to "free"

        # --8<-- [start:mutation-guard]
        free = Customer.where(lambda customer: customer.plan == "free")
        try:
            await free.limit(10).delete()
        except ValueError:
            # Portable SQL has no DELETE ... LIMIT. Fetch primary keys first,
            # then mutate by primary-key set:
            doomed = await free.limit(10).all()
            ids = [customer.id for customer in doomed]
            removed = await Customer.where(lambda customer: customer.id.in_(ids)).delete()
            assert removed == 1
        # --8<-- [end:mutation-guard]

    print("mutations example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
