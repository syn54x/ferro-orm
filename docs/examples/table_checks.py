"""Runnable companion for table checks (docs/pages/guide/models-and-fields.md).

A payment carries a card token or a bank account, never both. The SQL block
on the page is the ``DDL`` section below, checked against the statement
``create_tables()`` ran.
"""

import asyncio
import re
from typing import ClassVar

from ferro import (
    Check,
    CheckViolationError,
    Field,
    Model,
    connect,
    create_tables,
    engines,
)


# --8<-- [start:models]
class Payment(Model):
    __ferro_checks__: ClassVar[tuple[Check, ...]] = (
        Check(
            "at_most_one_method",
            lambda payment: (
                (payment.card_token == None)  # noqa: E711
                | (payment.bank_account == None)  # noqa: E711
            ),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    card_token: str | None = None
    bank_account: str | None = None


# --8<-- [end:models]

DDL = """
# --8<-- [start:ddl]
CREATE TABLE IF NOT EXISTS "payment" (
  "bank_account" varchar,
  "card_token" varchar,
  "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
  CONSTRAINT "ck_payment_at_most_one_method" CHECK (("card_token" IS NULL) OR ("bank_account" IS NULL))
);
# --8<-- [end:ddl]
"""


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").strip()


async def main() -> None:
    await connect("sqlite::memory:")
    report = await create_tables()
    [created] = [statement.sql for statement in report.statements]
    expected = "\n".join(line for line in DDL.splitlines() if "--8<--" not in line)
    assert _normalized(created) == _normalized(expected), created

    async with engines.session():
        await Payment.create(card_token="tok_visa_4242")
        await Payment.create(bank_account="GB33BUKB20201555555555")

        # --8<-- [start:violation]
        try:
            await Payment.create(
                card_token="tok_visa_4242",
                bank_account="GB33BUKB20201555555555",
            )
        except CheckViolationError as exc:
            assert "ck_payment_at_most_one_method" in str(exc)
        # --8<-- [end:violation]
        else:
            raise AssertionError("expected CheckViolationError")

        assert await Payment.select().count() == 2

    print("table_checks example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
