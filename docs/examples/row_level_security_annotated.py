"""Annotated-style companion to row_level_security.py (AGENTS.md I-7)."""

import asyncio
import uuid
import warnings
from typing import Annotated, ClassVar

from ferro import FerroField, Model, RowPolicy, RowSecurity, connect, engines


# --8<-- [start:models]
class Invoice(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
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

    skip_warnings = [w for w in caught if "PostgreSQL-only" in str(w.message)]
    assert len(skip_warnings) == 1, [str(w.message) for w in caught]
    assert "Invoice".lower() in str(skip_warnings[0].message).lower()

    async with engines.session():
        await Invoice.create(ledger_id=uuid.uuid4(), total=100)
        assert len(await Invoice.all()) == 1

    print("row_level_security_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
