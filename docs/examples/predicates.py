"""Runnable companion to the Queries guide (docs/pages/guide/queries.md)."""

import asyncio

# --8<-- [start:setup]
from ferro import Field, Model, connect
from ferro.query import col


class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    age: int
    role: str = "member"
    archived: bool = False
# --8<-- [end:setup]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    await User.bulk_create(
        [
            User(name="alice", age=34, role="admin"),
            User(name="bob", age=19),
            User(name="carol", age=42, archived=True),
            User(name="dave", age=17),
        ]
    )

    # --8<-- [start:filtering]
    adults = await User.where(lambda user: user.age >= 18).all()
    # --8<-- [end:filtering]
    assert len(adults) == 3

    # --8<-- [start:operator-style]
    # Deprecated path (planned removal: v0.14.0).
    adults = await User.where(User.age >= 18).all()
    # --8<-- [end:operator-style]
    assert len(adults) == 3

    # --8<-- [start:col-style]
    active = await User.where(col(User.archived) == False).all()  # noqa: E712
    # --8<-- [end:col-style]
    assert len(active) == 3

    # --8<-- [start:lambda-style]
    admins = await User.where(lambda user: (user.role == "admin") & (user.archived == False)).all()  # noqa: E712
    # --8<-- [end:lambda-style]
    assert len(admins) == 1

    # --8<-- [start:operators]
    teens = await User.where(lambda user: (user.age >= 13) & (user.age <= 19)).all()
    a_names = await User.where(lambda user: user.name.like("a%")).all()
    staff = await User.where(lambda user: user.role.in_(["admin", "moderator"])).all()
    # --8<-- [end:operators]
    assert len(teens) == 2
    assert len(a_names) == 1
    assert len(staff) == 1

    # --8<-- [start:combining]
    # & is AND, | is OR — parenthesize each side
    flagged = await User.where(lambda user: (user.age < 18) | (user.archived == True)).all()  # noqa: E712

    # Chained .where() calls also AND together
    young_members = await User.where(lambda user: user.role == "member").where(lambda user: user.age < 21).all()
    # --8<-- [end:combining]
    assert len(flagged) == 2
    assert len(young_members) == 2

    # --8<-- [start:ordering-slicing]
    oldest_first = await User.select().order_by(User.age, "desc").all()
    second_page = await User.select().order_by(User.id).limit(2).offset(2).all()
    # --8<-- [end:ordering-slicing]
    assert oldest_first[0].name == "carol"
    assert len(second_page) == 2

    # --8<-- [start:terminals]
    everyone = await User.all()
    first_admin = await User.where(lambda user: user.role == "admin").first()
    headcount = await User.select().count()
    any_minors = await User.where(lambda user: user.age < 18).exists()
    # --8<-- [end:terminals]
    assert len(everyone) == 4
    assert first_admin is not None
    assert headcount == 4
    assert any_minors

    print("predicates example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
