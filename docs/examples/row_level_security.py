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


async def main() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await connect("sqlite::memory:", auto_migrate=True)

    # SQLite has no row-level security: the model registers and the table is
    # created, unpoliced, with a warning naming exactly that.
    skip_warnings = [w for w in caught if "PostgreSQL-only" in str(w.message)]
    assert len(skip_warnings) == 1, [str(w.message) for w in caught]
    assert "Invoice".lower() in str(skip_warnings[0].message).lower()

    async with engines.session():
        await Invoice.create(ledger_id=uuid.uuid4(), total=100)
        assert len(await Invoice.all()) == 1

    print("row_level_security example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
