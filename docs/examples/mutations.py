"""Runnable companion to the Mutations guide (docs/pages/guide/mutations.md)."""

import asyncio

from ferro import Field, Model, connect


class Customer(Model):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True)
    name: str = ""
    plan: str = "free"


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

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
    await Customer.where(lambda t: t.email == "alice@example.com").update(name="Alice L.")
    await customer.refresh()
    assert customer.name == "Alice L."
    # --8<-- [end:refresh]

    print("mutations example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
