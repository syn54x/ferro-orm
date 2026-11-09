"""Runnable companion for the Upgrade guide's explicit foreign-key section
(docs/pages/upgrading.md).

``Note``, ``Pin`` and ``Tag`` reference the guide's ``Account`` model through
foreign-key columns, the form the section converts to. A key column whose
storage disagrees with its target key's is refused, naming the ``db_type`` to
add. The Annotated twin is ``upgrade_foreign_keys_annotated.py``.
"""

import asyncio

from upgrade_account import Account

from ferro import (
    Field,
    Forward,
    ManyToMany,
    Model,
    Relation,
    Reverse,
    connect,
    engines,
)


# --8<-- [start:models]
class Note(Model):
    id: int | None = Field(default=None, primary_key=True)
    body: str
    account_id: int = Field(foreign_key=Account, index=True)
    account: Relation[Account] = Forward("account_id")
    reply_to_id: int | None = Field(
        default=None, foreign_key="Note", on_delete="SET NULL"
    )
    reply_to: Relation["Note | None"] = Forward("reply_to_id")
    replies: Relation[list["Note"]] = Reverse("reply_to_id")
    pin: Relation[list["Pin"]] = Reverse("note_id")
    tags: Relation[list["Tag"]] = Reverse("notes")


class Pin(Model):
    id: int | None = Field(default=None, primary_key=True)
    note_id: int = Field(foreign_key=Note, unique=True)
    note: Relation[Note] = Forward("note_id")


class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str
    notes: Relation[list[Note]] = ManyToMany(through="tag_notes")


# --8<-- [end:models]


def storage_refusal() -> None:
    """Define a key column whose storage disagrees with its target key's."""
    # --8<-- [start:storage]
    try:

        class Ledger(Model):
            id: int | None = Field(default=None, primary_key=True, db_type="bigint")

        class Entry(Model):
            id: int | None = Field(default=None, primary_key=True)
            ledger_id: int = Field(foreign_key=Ledger)  # an integer column
            ledger: Relation[Ledger] = Forward("ledger_id")

    except TypeError as error:
        print(f"{type(error).__name__}: {error}")
        assert 'db_type="bigint"' in str(error)
    else:
        raise AssertionError("an integer key column beside a bigint key was accepted")
    # --8<-- [end:storage]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        ada = await Account.create(name="Ada Lovelace", email="ada@example.com", age=36)
        draft = await Note.create(body="First draft", account_id=ada.id)
        await Pin.create(note_id=draft.id)
        note = await Note.where(lambda note: note.body == "First draft").first()
        assert note is not None
        assert (await note.account.load()).name == "Ada Lovelace"
        assert (await note.pin.first()) is not None


if __name__ == "__main__":
    storage_refusal()
    asyncio.run(main())
