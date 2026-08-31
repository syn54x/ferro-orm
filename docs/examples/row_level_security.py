"""Runnable companion for row-level security (docs/pages/guide/row-level-security.md).

Row security is a PostgreSQL-only feature (docs/examples scripts run without a
live Postgres, so this one runs on SQLite): a model declaring ``__ferro_rls__``
still registers and migrates there — with a loud warning — so the model works
everywhere, and the enforcement itself is exercised by
``tests/test_rls_end_to_end.py`` against a real PostgreSQL role.
"""

import asyncio
import uuid
import warnings
from typing import ClassVar

from ferro import Field, Model, RowPolicy, RowSecurity, connect, engines


# --8<-- [start:models]
class Invoice(Model):
    id: int | None = Field(default=None, primary_key=True)
    ledger_id: uuid.UUID
    total: int

    __ferro_rls__: ClassVar = RowSecurity(
        RowPolicy(column="ledger_id", setting="pinch.ledger_id")
    )


# --8<-- [end:models]


# --8<-- [start:multi_policy]
class Doc(Model):
    id: int | None = Field(default=None, primary_key=True)
    ledger_id: uuid.UUID
    owner: str
    title: str

    __ferro_rls__: ClassVar = RowSecurity(
        # RESTRICTIVE: AND-composes with everything below. No ledger scope,
        # no rows — whoever you are.
        RowPolicy(
            name="tenant", column="ledger_id", setting="pinch.ledger_id",
            restrictive=True,
        ),
        # Permissive, unscoped by command: the owner reads and writes.
        RowPolicy(
            name="owner_all",
            using="\"owner\" = NULLIF(current_setting('pinch.member', true), '')",
            with_check="\"owner\" = NULLIF(current_setting('pinch.member', true), '')",
        ),
        # Permissive, SELECT-only: an invited member reads and nothing more.
        RowPolicy(
            name="invitee_read",
            command="select",
            using=(
                '"id" IN (SELECT doc_id FROM membership WHERE member = '
                "NULLIF(current_setting('pinch.member', true), ''))"
            ),
        ),
    )


# --8<-- [end:multi_policy]


async def main() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await connect("sqlite::memory:", auto_migrate=True)

    # SQLite has no row-level security: both models register and their
    # tables are created, unpoliced, each with a warning naming exactly that.
    skip_warnings = [str(w.message) for w in caught if "PostgreSQL-only" in str(w.message)]
    assert len(skip_warnings) == 2, skip_warnings
    assert any("invoice" in message.lower() for message in skip_warnings)
    assert any("doc" in message.lower() for message in skip_warnings)

    async with engines.session():
        await Invoice.create(ledger_id=uuid.uuid4(), total=100)
        assert len(await Invoice.all()) == 1

    print("row_level_security example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
