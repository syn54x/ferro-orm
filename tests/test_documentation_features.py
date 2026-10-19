"""Documented behaviour that no docs example exercises.

Every other documented feature runs in the example scripts under
``docs/examples`` (``tests/test_docs_examples.py``) or in a page block marked
``{test=run prelude=<name>}``. What remains here is documented but shown by
no runnable example: an ``|`` predicate, and two tasks committing their own
transactions at once. Both run on every backend.
"""

import asyncio
from enum import Enum
from typing import Annotated

import pytest

from ferro import FerroField, Model, connect, engines, transaction

pytestmark = pytest.mark.backend_matrix


class UserRole(Enum):
    USER = "user"
    ADMIN = "admin"
    MODERATOR = "moderator"


class DocUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    username: Annotated[str, FerroField(unique=True)]
    email: Annotated[str, FerroField(unique=True, index=True)]
    role: UserRole = UserRole.USER


@pytest.fixture(autouse=True)
def _ensure_models_registered():
    """Re-register the model: another test's fixture may clear the registry."""
    from ferro.registry import REGISTRY

    REGISTRY.register(DocUser)
    yield


@pytest.mark.asyncio
async def test_logical_or_operator(db_url):
    """An ``|`` predicate matches rows that meet either side."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await DocUser.create(
            username="alice", email="alice@example.com", role=UserRole.ADMIN
        )
        await DocUser.create(
            username="bob", email="bob@example.com", role=UserRole.MODERATOR
        )
        await DocUser.create(
            username="charlie", email="charlie@example.com", role=UserRole.USER
        )

        staff = await DocUser.where(
            lambda doc_user: (
                (doc_user.role == UserRole.ADMIN.value)
                | (doc_user.role == UserRole.MODERATOR.value)
            )
        ).all()
        assert sorted(user.username for user in staff) == ["alice", "bob"]


@pytest.mark.asyncio
async def test_concurrent_transactions_both_commit(db_url):
    """Two tasks in one session each commit their own transaction."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():

        async def task_a():
            async with transaction():
                await DocUser.create(username="task_a_user", email="a@example.com")
                await asyncio.sleep(0.1)

        async def task_b():
            async with transaction():
                await DocUser.create(username="task_b_user", email="b@example.com")

        await asyncio.gather(task_a(), task_b())

        users = await DocUser.all()
        assert sorted(user.username for user in users) == [
            "task_a_user",
            "task_b_user",
        ]
