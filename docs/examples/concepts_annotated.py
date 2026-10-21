"""Runnable companion to the Concepts pages (docs/pages/concepts/).

Declares the ``User`` model that the architecture, identity map, type safety
and performance pages share, adds four users and runs the blocks those pages
embed: the query the architecture page follows through each layer, one row
resolving to one instance, sessionless loads, a connection with the
identity map turned off, and the backends page's connect-and-query block. The ``concepts`` prelude
(``_preludes/concepts.py``) imports the model and the rows from here for the
pages' inline blocks.
"""

import asyncio
from typing import Annotated

# --8<-- [start:models]
from ferro import Field, Model


class User(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    username: Annotated[str, Field(unique=True)]
    email: str
    age: int
    active: bool = True


# --8<-- [end:models]

from ferro import connect, engines, reset_engine  # noqa: E402

USERS = [
    {"username": "ada", "email": "ada@example.com", "age": 36},
    {"username": "grace", "email": "grace@example.com", "age": 45},
    {"username": "alan", "email": "alan@example.com", "age": 17},
    {"username": "edsger", "email": "edsger@example.com", "age": 52, "active": False},
]


async def seed(model: type[User] = User) -> None:
    """Add the four users, in their own session."""
    async with engines.session():
        await model.bulk_create([model(**row) for row in USERS])


async def query() -> None:
    # --8<-- [start:query]
    from ferro import engines

    async with engines.session():
        users = await (
            User.where(lambda user: user.active == True)
            .order_by(lambda user: user.age, "desc")
            .limit(10)
            .all()
        )
        assert [user.username for user in users] == ["grace", "ada", "alan"]
    # --8<-- [end:query]


async def refresh_on_load() -> None:
    # --8<-- [start:refresh-on-load]
    from ferro import engines, execute

    async with engines.session():
        ada = await User.get(1)

        # Something else (another request, a script, raw SQL) changes the
        # row without going through `ada`.
        await execute('UPDATE "user" SET email = ? WHERE id = ?', "ada@lovelace.dev", 1)

        again = await User.get(1)
        assert again is ada  # the same object
        assert ada.email == "ada@lovelace.dev"  # refreshed in place

        # An unsaved edit does not survive a re-fetch: the database wins.
        ada.email = "unsaved edit"
        await User.get(1)
        assert ada.email == "ada@lovelace.dev"
    # --8<-- [end:refresh-on-load]


async def sessionless() -> None:
    # --8<-- [start:sessionless]
    # `using=` alone: no session, so no identity map.
    first = await User.using("default").get(2)
    second = await User.using("default").get(2)
    assert first is not second
    assert first.email == second.email == "grace@example.com"

    # No session and no `using=`: Ferro has no route for the call.
    try:
        await User.get(2)
    except RuntimeError as error:
        assert str(error).startswith("No database route for this operation.")
    else:
        raise AssertionError("a call with no route must raise RuntimeError")
    # --8<-- [end:sessionless]


async def without_identity_map() -> None:
    reset_engine()
    # --8<-- [start:opt-out]
    from ferro import connect, engines

    await connect("sqlite::memory:", auto_migrate=True, identity_map=False)

    async with engines.session():
        ada = await User.create(username="ada", email="ada@example.com", age=36)
        first = await User.get(ada.id)
        second = await User.get(ada.id)
        assert first is not ada
        assert first is not second  # no map, even inside a session
    # --8<-- [end:opt-out]


async def backends(url: str = "sqlite::memory:") -> None:
    reset_engine()
    # --8<-- [start:backends]
    from ferro import connect, engines

    await connect(url, auto_migrate=True)

    async with engines.session():
        await User.create(username="ada", email="ada@example.com", age=36)
        adults = await User.where(lambda user: user.age >= 18).all()
        assert [user.username for user in adults] == ["ada"]
    # --8<-- [end:backends]


async def unsupported_scheme() -> None:
    reset_engine()
    # --8<-- [start:unsupported-scheme]
    from ferro import InterfaceError, connect

    try:
        await connect("mysql://user:password@localhost:3306/app")
    except InterfaceError as error:
        print(f"InterfaceError: {error}")
    else:
        raise AssertionError("expected InterfaceError")
    # --8<-- [end:unsupported-scheme]


async def main() -> None:
    # --8<-- [start:connect]
    await connect("sqlite::memory:", auto_migrate=True)
    # --8<-- [end:connect]
    await seed()
    await query()
    await refresh_on_load()
    await sessionless()
    await without_identity_map()
    await backends()
    await unsupported_scheme()
    print("concepts: ok")


if __name__ == "__main__":
    asyncio.run(main())
