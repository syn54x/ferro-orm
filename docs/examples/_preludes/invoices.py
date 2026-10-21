"""Invoices of two tenants, for the docstrings of session settings.

A block marked ``{test=run prelude=invoices backend=postgres}`` runs on
PostgreSQL, where a session can send settings (``set_config``). ``opened()``
connects, adds three invoices and does not open a session: the block opens
its own, with the settings it is about. ``resolve_tenant(request)`` stands for
the application's own lookup and returns ``"acme"``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import ferro
from ferro import Field, Model, connect, engines


class Invoice(Model):
    id: int | None = Field(default=None, primary_key=True)
    tenant: str
    total: float


request = object()
"""The incoming request a web handler would hold."""


async def resolve_tenant(request: object) -> str:
    """The tenant the request belongs to."""
    return "acme"


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect and add the invoices; the block opens its own session."""
    await connect("postgresql://localhost/app", auto_migrate=True)
    try:
        async with engines.session():
            await Invoice.create(tenant="acme", total=250.0)
            await Invoice.create(tenant="acme", total=40.0)
            await Invoice.create(tenant="globex", total=900.0)
        yield
    finally:
        ferro.reset_engine()
