"""A refused class definition leaves nothing behind (#812).

A model whose definition raises `TypeError` never existed as far as Ferro is
concerned: the registry, the deferred relations and the registered schema are
as they were before the class statement, so the next `connect()` neither
raises the refusal again nor creates a table for it:

```python
try:
    class Rate(Model):
        id: int | None = Field(default=None, primary_key=True)
        price: float
        doubled: float = Field(generated=lambda rate: rate.price * 2)
except TypeError as refusal:
    print(refusal)              # Rate.doubled '*' over a float: ...

await ferro.connect(url, auto_migrate=True)   # creates "ledger" only
```
"""

from collections.abc import Callable
from typing import Annotated, ClassVar

import pytest

import ferro
from ferro import Check, Field, ForeignKey, Model
from ferro.registry import REGISTRY
from tests._pass_harness import auto_migrate, schema_steps

pytestmark = [
    pytest.mark.usefixtures("clean_registry"),
    pytest.mark.backend_matrix,
]


def define_ledger() -> type[Model]:
    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    return Ledger


def refused_by_value_parity() -> None:
    class Rate(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger: Annotated["Ledger", ForeignKey(related_name="rates")]
        price: float
        doubled: float = Field(generated=lambda rate: rate.price * 2)


def refused_by_db_type() -> None:
    class Rate(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger: Annotated["Ledger", ForeignKey(related_name="rates")]
        name: str = Field(db_type="bigint")


def refused_by_check() -> None:
    class Rate(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("only_one", lambda rate: rate.nonexistent == None),  # noqa: E711
        )

        id: int | None = Field(default=None, primary_key=True)
        ledger: Annotated["Ledger", ForeignKey(related_name="rates")]
        left: str | None = None


REFUSALS = {
    "value parity": (refused_by_value_parity, r"Rate\.doubled '\*' over a float"),
    "db_type option": (refused_by_db_type, r"name.*db_type"),
    "table check": (refused_by_check, "nonexistent"),
}


@pytest.fixture(params=list(REFUSALS))
def refusal(request) -> tuple[Callable[[], None], str]:
    return REFUSALS[request.param]


def test_a_refused_class_leaves_the_registry_as_it_was(refusal):
    define, message = refusal
    define_ledger()
    before = REGISTRY.snapshot()

    with pytest.raises(TypeError, match=message):
        define()

    assert REGISTRY.snapshot() == before
    assert not any(key.endswith(".Rate") for key in REGISTRY.models())


@pytest.mark.asyncio
async def test_connect_after_a_refused_class_creates_nothing_for_it(db_url, refusal):
    define, message = refusal
    define_ledger()
    with pytest.raises(TypeError, match=message):
        define()

    report = await auto_migrate(db_url)

    assert {subject for subject, _ in schema_steps(report)} == {"ledger"}
    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)


def test_a_valid_class_on_the_same_table_after_a_refused_one_registers(refusal):
    define, message = refusal
    define_ledger()
    with pytest.raises(TypeError, match=message):
        define()

    def define_rate() -> type[Model]:
        class Rate(Model):
            id: int | None = Field(default=None, primary_key=True)
            price: float

        return Rate

    rate = define_rate()

    assert REGISTRY.resolve_reference("Rate") is rate


def test_a_valid_class_of_the_same_identity_after_a_refused_one_registers():
    with pytest.raises(TypeError, match=r"Rate\.doubled"):

        class Rate(Model):
            id: int | None = Field(default=None, primary_key=True)
            price: float
            doubled: float = Field(generated=lambda rate: rate.price * 2)

    class Rate(Model):  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        price: int
        doubled: int = Field(generated=lambda rate: rate.price * 2)

    assert REGISTRY.models()[Rate.__ferro_identity__] is Rate
