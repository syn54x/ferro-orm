"""Annotated-style companion to table_checks.py (AGENTS.md I-7)."""

import asyncio
from typing import Annotated, ClassVar

from ferro import Check, CheckViolationError, Field, Model, connect, engines


# --8<-- [start:models]
class Payment(Model):
    __ferro_checks__: ClassVar[tuple[Check, ...]] = (
        Check(
            "at_most_one_method",
            lambda payment: (
                (payment.card_token == None) | (payment.bank_account == None)
            ),
        ),
    )

    id: Annotated[int | None, Field(primary_key=True)] = None
    card_token: str | None = None
    bank_account: str | None = None


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await Payment.create(card_token="tok_visa_4242")

        try:
            await Payment.create(
                card_token="tok_visa_4242",
                bank_account="GB33BUKB20201555555555",
            )
        except CheckViolationError as exc:
            assert "ck_payment_at_most_one_method" in str(exc)
        else:
            raise AssertionError("expected CheckViolationError")

    print("table_checks_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
