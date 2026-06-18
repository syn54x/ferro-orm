"""Annotated-style companion to relationships.py (AGENTS.md I-8).

Field options move into ``Annotated[...]``. The relationship declarations
themselves are identical in both styles: forward FKs are always
``Annotated[Target, ForeignKey(...)]`` and ``BackRef()``/``ManyToMany()``
are always assignments.
"""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation, connect


# --8<-- [start:one-to-many]
class Team(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    members: Relation[list["Player"]] = BackRef()


class Player(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    team: Annotated[Team, ForeignKey(related_name="members")]
# --8<-- [end:one-to-many]


# --8<-- [start:one-to-one]
class User(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    username: str
    profile: "Profile" = BackRef()


class Profile(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    bio: str
    user: Annotated[User, ForeignKey(related_name="profile", unique=True)]
# --8<-- [end:one-to-one]


# --8<-- [start:many-to-many]
class Student(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    courses: Relation[list["Course"]] = ManyToMany(related_name="students")


class Course(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    title: str
    students: Relation[list["Student"]] = BackRef()
# --8<-- [end:many-to-many]


# --8<-- [start:self-referential]
class Employee(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    manager: Annotated["Employee", ForeignKey(related_name="reports", nullable=True)] = None
    reports: Relation[list["Employee"]] = BackRef()
# --8<-- [end:self-referential]


# --8<-- [start:on-delete]
class Library(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    documents: Relation[list["Document"]] = BackRef()


class Document(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    title: str
    library: Annotated[Library, ForeignKey(related_name="documents", on_delete="CASCADE")]
# --8<-- [end:on-delete]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    team = await Team.create(name="Rustaceans")
    player = await Player.create(name="Ferris", team=team)
    assert (await player.team).id == team.id

    user = await User.create(username="alice")
    await Profile.create(bio="Pythonista", user=user)
    assert (await user.profile).bio == "Pythonista"

    sam = await Student.create(name="Sam")
    rust101 = await Course.create(title="Rust 101")
    await sam.courses.add(rust101)
    assert len(await sam.courses.all()) == 1

    boss = await Employee.create(name="Grace")
    dev = await Employee.create(name="Linus", manager=boss)
    assert (await dev.manager).name == "Grace"

    lib = await Library.create(name="Main")
    await Document.create(title="Charter", library=lib)
    assert len(await lib.documents.all()) == 1

    print("relationships_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
