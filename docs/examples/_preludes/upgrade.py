"""The running example of the Upgrade guide.

The ``Account`` model of ``docs/examples/upgrade_account.py`` and four rows,
and the ``Note``, ``Pin`` and ``Tag`` models of
``docs/examples/upgrade_foreign_keys.py`` with a few rows of their own, in an
in-memory SQLite database. A block marked ``{test=run prelude=upgrade}`` runs
inside ``opened()`` and can use ``Account``, ``Role``, ``Note``, ``Pin``,
``Tag``, ``datetime``, ``UTC``, ``ferro`` and ``engines`` directly.

Ada Lovelace (36, admin, last seen 2026-03-01), Grace Hopper (45, never seen),
Alan Turing (41, inactive, last seen 2026-02-01) and Linus Torvalds (28, last
seen 2026-03-05). Every other account is an active member.

Ada's note "First draft" and Grace's note "Reply", which replies to it. A
``Pin`` on Ada's note, and a ``Tag`` "draft" on both notes.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime  # noqa: F401 - names the blocks use
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from upgrade_account import Account, Role  # noqa: E402,F401
from upgrade_foreign_keys import Note, Pin, Tag  # noqa: E402,F401

import ferro  # noqa: E402,F401
from ferro import connect, engines  # noqa: E402,F401


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect, add the rows and hold a session open around the block."""
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        ada = await Account.create(
            name="Ada Lovelace",
            email="ada@example.com",
            role=Role.ADMIN,
            age=36,
            seen_at=datetime(2026, 3, 1, 15, 0, tzinfo=UTC),
        )
        grace = await Account.create(
            name="Grace Hopper", email="grace@example.com", age=45
        )
        await Account.create(
            name="Alan Turing",
            email="alan@example.com",
            age=41,
            active=False,
            seen_at=datetime(2026, 2, 1, 9, 30, tzinfo=UTC),
        )
        await Account.create(
            name="Linus Torvalds",
            email="linus@example.com",
            age=28,
            seen_at=datetime(2026, 3, 5, 18, 0, tzinfo=UTC),
        )
        draft = await Note.create(body="First draft", account_id=ada.id)
        reply = await Note.create(
            body="Reply", account_id=grace.id, reply_to_id=draft.id
        )
        await Pin.create(note_id=draft.id)
        tag = await Tag.create(label="draft")
        await tag.notes.add(draft, reply)
        yield
