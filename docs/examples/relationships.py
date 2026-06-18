"""Runnable companion to the Relationships guide (docs/pages/guide/relationships.md)."""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation, connect


# --8<-- [start:one-to-many]
class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    members: Relation[list["Player"]] = BackRef()


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    team: Annotated[Team, ForeignKey(related_name="members")]
# --8<-- [end:one-to-many]


# --8<-- [start:one-to-one]
class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    username: str
    profile: "Profile" = BackRef()


class Profile(Model):
    id: int | None = Field(default=None, primary_key=True)
    bio: str
    user: Annotated[User, ForeignKey(related_name="profile", unique=True)]
# --8<-- [end:one-to-one]


# --8<-- [start:many-to-many]
class Student(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    courses: Relation[list["Course"]] = ManyToMany(related_name="students")


class Course(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    students: Relation[list["Student"]] = BackRef()
# --8<-- [end:many-to-many]


# --8<-- [start:self-referential]
class Employee(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    manager: Annotated["Employee", ForeignKey(related_name="reports", nullable=True)] = None
    reports: Relation[list["Employee"]] = BackRef()
# --8<-- [end:self-referential]


# --8<-- [start:on-delete]
class Library(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    documents: Relation[list["Document"]] = BackRef()


class Document(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    library: Annotated[Library, ForeignKey(related_name="documents", on_delete="CASCADE")]
# --8<-- [end:on-delete]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    # --8<-- [start:one-to-many-usage]
    team = await Team.create(name="Rustaceans")
    crab = await Player.create(name="Ferris", team=team)

    # Forward: awaiting the FK field loads the related instance
    assert (await crab.team).name == "Rustaceans"

    # The shadow column is available for direct reads and filters
    assert crab.team_id == team.id

    # Reverse: the BackRef is a chainable query
    roster = await team.members.order_by(Player.name).all()
    # --8<-- [end:one-to-many-usage]
    assert len(roster) == 1

    # --8<-- [start:one-to-one-usage]
    user = await User.create(username="alice")
    await Profile.create(bio="Pythonista", user=user)

    profile = await user.profile  # single instance, not a list
    # --8<-- [end:one-to-one-usage]
    assert profile.bio == "Pythonista"

    # --8<-- [start:m2m-usage]
    sam = await Student.create(name="Sam")
    rust101 = await Course.create(title="Rust 101")
    python201 = await Course.create(title="Python 201")

    await sam.courses.add(rust101, python201)
    assert len(await sam.courses.all()) == 2

    # The reverse side works the same way
    assert len(await rust101.students.all()) == 1

    await sam.courses.remove(python201)
    await sam.courses.clear()
    # --8<-- [end:m2m-usage]
    assert len(await sam.courses.all()) == 0

    # --8<-- [start:self-referential-usage]
    boss = await Employee.create(name="Grace")
    dev = await Employee.create(name="Linus", manager=boss)

    assert (await dev.manager).name == "Grace"
    assert len(await boss.reports.all()) == 1
    # --8<-- [end:self-referential-usage]

    print("relationships example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
