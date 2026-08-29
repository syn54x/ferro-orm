"""Runnable companion to the Queries guide (docs/pages/guide/queries.md)."""

import asyncio
from datetime import UTC, datetime

# --8<-- [start:setup]
from ferro import Field, Model, connect, engines


class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    age: int
    role: str = "member"
    archived: bool = False
# --8<-- [end:setup]


# --8<-- [start:nullable-model]
class Invoice(Model):
    id: int | None = Field(default=None, primary_key=True)
    reference: str
    amount: float | None = None  # None until the invoice is issued
# --8<-- [end:nullable-model]


# --8<-- [start:card-model]
class Card(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    pinned_at: datetime | None = None
    updated_at: datetime
# --8<-- [end:card-model]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
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

        # --8<-- [start:negation]
        # ~ negates ANY predicate: a comparison, .in_(), .like(), or a whole
        # &/| group. There are no per-operator negative forms to memorize.
        not_staff = await User.where(lambda user: ~user.role.in_(["admin", "moderator"])).all()
        not_a_names = await User.where(lambda user: ~user.name.like("a%")).all()
        active_adults = await User.where(lambda user: ~((user.age < 18) | (user.archived == True))).all()  # noqa: E712
        # --8<-- [end:negation]
        assert len(not_staff) == 3
        assert len(not_a_names) == 3
        assert len(active_adults) == 2

        await Invoice.bulk_create(
            [
                Invoice(reference="inv-1", amount=250.0),
                Invoice(reference="inv-2", amount=40.0),
                Invoice(reference="inv-3", amount=None),
            ]
        )

        # --8<-- [start:three-valued]
        big = await Invoice.where(lambda invoice: invoice.amount > 100).all()
        not_big = await Invoice.where(lambda invoice: ~(invoice.amount > 100)).all()

        # inv-3 (amount is NULL) appears in NEITHER list
        assert {invoice.reference for invoice in big} == {"inv-1"}
        assert {invoice.reference for invoice in not_big} == {"inv-2"}

        # keeping the NULL rows is an explicit extra condition
        not_big_or_unissued = await Invoice.where(
            lambda invoice: ~(invoice.amount > 100) | (invoice.amount == None)  # noqa: E711
        ).all()
        assert {invoice.reference for invoice in not_big_or_unissued} == {"inv-2", "inv-3"}
        # --8<-- [end:three-valued]

        # --8<-- [start:ordering-slicing]
        oldest_first = await User.select().order_by(lambda user: user.age, "desc").all()
        second_page = (
            await User.select().order_by(lambda user: user.id).limit(2).offset(2).all()
        )
        # --8<-- [end:ordering-slicing]
        assert oldest_first[0].name == "carol"
        assert len(second_page) == 2

        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        t1 = datetime(2026, 2, 1, tzinfo=UTC)
        t2 = datetime(2026, 3, 1, tzinfo=UTC)
        await Card.bulk_create(
            [
                Card(title="unpinned-old", pinned_at=None, updated_at=t0),
                Card(title="pinned-early", pinned_at=t0, updated_at=t0),
                Card(title="unpinned-new", pinned_at=None, updated_at=t2),
                Card(title="pinned-late", pinned_at=t1, updated_at=t1),
            ]
        )

        # --8<-- [start:nulls-ordering]
        cards = (
            await Card.select()
            .order_by(lambda card: card.pinned_at, "desc", nulls="last")
            .order_by(lambda card: card.updated_at, "desc")
            .order_by(lambda card: card.id, "desc")
            .all()
        )
        # --8<-- [end:nulls-ordering]
        assert [card.title for card in cards] == [
            "pinned-late",
            "pinned-early",
            "unpinned-new",
            "unpinned-old",
        ]

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
