"""Annotated-style companion to row_level_security.py (AGENTS.md I-7)."""

import asyncio
import uuid
import warnings
from typing import Annotated, ClassVar

from ferro import Field, Model, RowPolicy, RowSecurity, connect, engines


# --8<-- [start:models]
class Invoice(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    tenant_id: uuid.UUID
    total: int

    __ferro_rls__: ClassVar = RowSecurity(
        RowPolicy(column="tenant_id", setting="app.tenant_id")
    )


# --8<-- [end:models]


# --8<-- [start:multi_policy]
class Doc(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    tenant_id: uuid.UUID
    owner: str
    title: str

    __ferro_rls__: ClassVar = RowSecurity(
        # RESTRICTIVE: AND-composes with everything below. No tenant scope,
        # no rows — whoever you are.
        RowPolicy(
            name="tenant", column="tenant_id", setting="app.tenant_id",
            restrictive=True,
        ),
        # Permissive, unscoped by command: the owner reads and writes.
        RowPolicy(
            name="owner_all",
            using="\"owner\" = NULLIF(current_setting('app.current_user', true), '')",
            with_check="\"owner\" = NULLIF(current_setting('app.current_user', true), '')",
        ),
        # Permissive, SELECT-only: an invited member reads and nothing more.
        RowPolicy(
            name="invitee_read",
            command="select",
            using=(
                '"id" IN (SELECT doc_id FROM membership WHERE member = '
                "NULLIF(current_setting('app.current_user', true), ''))"
            ),
        ),
    )


# --8<-- [end:multi_policy]


async def main() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await connect("sqlite::memory:", auto_migrate=True)

    skip_warnings = [str(w.message) for w in caught if "PostgreSQL-only" in str(w.message)]
    assert len(skip_warnings) == 2, skip_warnings
    assert any("invoice" in message.lower() for message in skip_warnings)
    assert any("doc" in message.lower() for message in skip_warnings)

    async with engines.session():
        await Invoice.create(tenant_id=uuid.uuid4(), total=100)
        assert len(await Invoice.all()) == 1

    print("row_level_security_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
