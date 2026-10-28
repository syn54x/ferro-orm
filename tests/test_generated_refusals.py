"""Class-definition refusals for generated columns (value parity, ADR-0059).

A model whose generated expression could store a different value on SQLite
and Postgres, or fail on one and not the other, is refused when the class is
defined, and each refusal names what to write instead:

```python
class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
    amount_minor: int
    tip: int | None = None
    capped: int = Field(generated=lambda transaction: fn.least(transaction.tip, 28))
# TypeError: ... fn.least over the nullable column 'tip' ... fn.coalesce(...)
```

`fn` inside a query is refused too, pointing at generated columns, until views
bring `fn` to queries.
"""

import datetime
from decimal import Decimal
from enum import Enum

import pytest

from ferro import Field, ForeignKey, Model, fn
from typing import Annotated

pytestmark = pytest.mark.usefixtures("clean_registry")


# -- types: the expression's family must be the column's ---------------------------


def test_least_over_a_nullable_column_suggests_coalesce():
    with pytest.raises(TypeError, match=r"nullable .*'tip'.*fn\.coalesce"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            tip: int | None = None
            capped: int = Field(
                generated=lambda transaction: fn.least(transaction.tip, 28)
            )


def test_greatest_over_a_nullable_column_suggests_coalesce():
    with pytest.raises(TypeError, match=r"fn\.coalesce"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            tip: int | None = None
            floor: int = Field(
                generated=lambda transaction: fn.greatest(transaction.tip, 0)
            )


def test_least_over_coalesce_is_accepted():
    class Transaction(Model):
        id: int | None = Field(default=None, primary_key=True)
        tip: int | None = None
        capped: int = Field(
            generated=lambda transaction: fn.least(fn.coalesce(transaction.tip, 0), 28)
        )

    assert Transaction.__ferro_generated__ == {"capped": frozenset({"tip"})}


def test_day_of_a_datetime_is_refused_naming_date():
    with pytest.raises(TypeError, match=r"time zone.*date field"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            created_at: datetime.datetime
            day: int = Field(
                generated=lambda transaction: fn.day(transaction.created_at)
            )


def test_year_and_month_of_a_datetime_are_refused():
    with pytest.raises(TypeError, match="time zone"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            created_at: datetime.datetime
            year: int = Field(
                generated=lambda transaction: fn.year(transaction.created_at)
            )

    with pytest.raises(TypeError, match="time zone"):

        class Payment(Model):
            id: int | None = Field(default=None, primary_key=True)
            created_at: datetime.datetime
            month: int = Field(generated=lambda payment: fn.month(payment.created_at))


def test_a_number_expression_on_an_int_field_names_both():
    with pytest.raises(TypeError, match=r"number.*int"):

        class Line(Model):
            id: int | None = Field(default=None, primary_key=True)
            price: float
            total: int = Field(generated=lambda line: line.price * 2)


def test_an_integer_expression_on_a_float_field_names_both():
    with pytest.raises(TypeError, match=r"integer.*float"):

        class Line(Model):
            id: int | None = Field(default=None, primary_key=True)
            qty: int
            total: float = Field(generated=lambda line: line.qty * 2)


def test_a_db_type_inside_the_family_is_accepted():
    class Line(Model):
        id: int | None = Field(default=None, primary_key=True)
        qty: int
        double_qty: int = Field(generated=lambda line: line.qty * 2, db_type="smallint")

    assert Line.__ferro_generated__ == {"double_qty": frozenset({"qty"})}


def test_least_on_text_is_refused_naming_collation():
    with pytest.raises(TypeError, match="collation"):

        class Person(Model):
            id: int | None = Field(default=None, primary_key=True)
            first: str
            last: str
            earliest: str = Field(
                generated=lambda person: fn.least(person.first, person.last)
            )


def test_a_function_on_the_wrong_argument_type_lists_its_signatures():
    with pytest.raises(TypeError, match=r"fn\.length takes \(string\)"):

        class Line(Model):
            id: int | None = Field(default=None, primary_key=True)
            qty: int
            size: int = Field(generated=lambda line: fn.length(line.qty))


def test_decimal_arithmetic_is_refused_naming_floating_point():
    with pytest.raises(TypeError, match="floating point"):

        class Line(Model):
            id: int | None = Field(default=None, primary_key=True)
            price: Decimal
            doubled: Decimal = Field(generated=lambda line: line.price * 2)


def test_an_enum_field_cannot_be_generated():
    class Kind(Enum):
        a = "a"

    with pytest.raises(TypeError, match="Kind"):

        class Line(Model):
            id: int | None = Field(default=None, primary_key=True)
            label: str
            kind: Kind = Field(generated=lambda line: line.label)


# -- what the expression reads ------------------------------------------------------


def test_reading_another_generated_column_is_refused():
    with pytest.raises(TypeError, match=r"generated column 'direction'.*repeat"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            direction: int = Field(
                generated=lambda transaction: fn.sign(transaction.amount_minor)
            )
            flipped: int = Field(generated=lambda transaction: -transaction.direction)


def test_reading_a_retired_column_is_refused():
    with pytest.raises(TypeError, match=r"retired column 'legacy'"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            legacy: int | None = Field(default=None, retired=True)
            copied: int | None = Field(
                generated=lambda transaction: transaction.legacy + 1
            )


def test_reading_a_relation_is_refused_naming_the_shadow_column():
    class Author(Model):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(TypeError, match=r"relation 'author'.*'author_id'"):

        class Post(Model):
            id: int | None = Field(default=None, primary_key=True)
            author: Annotated[Author, ForeignKey(related_name="posts")]
            author_copy: int = Field(generated=lambda post: post.author)


# -- the body is a value, not a condition --------------------------------------------


def test_a_comparison_body_points_at_check():
    with pytest.raises(TypeError, match="Check"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            negative: bool = Field(
                generated=lambda transaction: transaction.amount_minor < 0
            )


def test_a_comparison_of_a_function_points_at_check():
    with pytest.raises(TypeError, match="Check"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            large: bool = Field(
                generated=lambda transaction: fn.abs(transaction.amount_minor) > 100
            )


def test_a_boolean_body_points_at_check():
    with pytest.raises(TypeError, match="Check"):

        class Account(Model):
            id: int | None = Field(default=None, primary_key=True)
            active: bool
            mirror: bool = Field(generated=lambda account: account.active)


# -- division -------------------------------------------------------------------------


def test_division_by_a_column_is_refused():
    with pytest.raises(TypeError, match="non-zero literal"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            count: int
            average: int = Field(
                generated=lambda transaction: (
                    transaction.amount_minor / transaction.count
                )
            )


def test_division_by_zero_is_refused():
    with pytest.raises(TypeError, match="non-zero literal"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            broken: int = Field(
                generated=lambda transaction: transaction.amount_minor / 0
            )


def test_division_by_a_literal_is_accepted():
    class Transaction(Model):
        id: int | None = Field(default=None, primary_key=True)
        amount_minor: int
        whole: int = Field(generated=lambda transaction: transaction.amount_minor / 100)

    assert Transaction.__ferro_generated__ == {"whole": frozenset({"amount_minor"})}


# -- options a generated column cannot take ------------------------------------------


@pytest.mark.parametrize(
    "options, named",
    [
        ({"primary_key": True}, "primary_key=True"),
        ({"autoincrement": True}, "autoincrement=True"),
        ({"default": 5}, "default="),
        ({"default_factory": int}, "default="),
    ],
)
def test_an_option_a_generated_column_cannot_take_is_refused(options, named):
    with pytest.raises(TypeError, match=rf"generated column.*{named}.*drop"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            direction: int = Field(
                generated=lambda transaction: fn.sign(transaction.amount_minor),
                **options,
            )


def test_db_check_on_a_generated_column_is_refused():
    class Kind(Enum):
        a = "a"

    with pytest.raises(TypeError, match=r"db_check=True.*drop"):

        class Line(Model):
            id: int | None = Field(default=None, primary_key=True)
            label: str
            kind: Kind = Field(
                generated=lambda line: line.label, db_type="text", db_check=True
            )


# -- fn in a query -------------------------------------------------------------------


def _declare_transaction() -> type[Model]:
    class Transaction(Model):
        id: int | None = Field(default=None, primary_key=True)
        amount_minor: int

    return Transaction


def test_fn_in_where_is_refused_naming_generated_columns():
    Transaction = _declare_transaction()
    with pytest.raises(TypeError, match="generated column"):
        Transaction.where(lambda transaction: fn.abs(transaction.amount_minor) > 5)
    with pytest.raises(TypeError, match="generated column"):
        Transaction.where(lambda transaction: fn.abs(transaction.amount_minor))


def test_fn_nested_in_arithmetic_in_a_query_is_refused():
    Transaction = _declare_transaction()
    with pytest.raises(TypeError, match="generated column"):
        Transaction.where(lambda transaction: fn.abs(transaction.amount_minor) + 1 > 5)


def test_fn_in_select_is_refused_naming_generated_columns():
    Transaction = _declare_transaction()
    with pytest.raises(TypeError, match="generated column"):
        Transaction.select(lambda transaction: fn.abs(transaction.amount_minor))
    with pytest.raises(TypeError, match="generated column"):
        Transaction.select(
            lambda transaction: {"size": fn.abs(transaction.amount_minor)}
        )


def test_fn_in_order_by_is_refused_naming_generated_columns():
    Transaction = _declare_transaction()
    with pytest.raises(TypeError, match="generated column"):
        Transaction.select().order_by(
            lambda transaction: fn.abs(transaction.amount_minor)
        )


@pytest.mark.asyncio
async def test_fn_in_an_update_recipe_is_refused_naming_generated_columns():
    Transaction = _declare_transaction()
    query = Transaction.where(lambda transaction: transaction.id == 1)
    with pytest.raises(TypeError, match="generated column"):
        await query.update(
            lambda transaction: {"amount_minor": fn.abs(transaction.amount_minor)}
        )
    with pytest.raises(TypeError, match="generated column"):
        await query.update(
            lambda transaction: {"amount_minor": fn.abs(transaction.amount_minor) + 1}
        )
    with pytest.raises(TypeError, match="generated column"):
        await query.update(amount_minor=fn.abs(5))
