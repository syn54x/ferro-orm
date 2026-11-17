"""Errors raised from a query lambda spell the row the way the lambda does.

`Sale.where(lambda sale: sum(sale.amount) > 10)` says `sale.amount`, not
`t.amount`; with no lambda to read, a message names the model in snake case
(`line_item` for `LineItem`). No such message cites an ADR or an issue number.
"""

import ast
import functools
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

import pytest

from ferro import (
    Check,
    Field,
    FerroField,
    Forward,
    Model,
    Relation,
    Reverse,
    connect,
    engines,
)
from ferro.relations import resolve_relationships

if TYPE_CHECKING:
    # Defined for the type checker only: reading these annotations at runtime
    # raises NameError, as it does in a module that imports them this way.
    from ferro.query.nodes import QueryNode, QueryProxy

_SRC = Path(__file__).resolve().parents[1] / "src" / "ferro"
_STALE = re.compile(r"\bt\.|ADR-\d+|#\d+")


@pytest.fixture()
def models(clean_registry: None) -> dict[str, type]:
    class Shop(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

        sales: Relation[list["Sale"]] = Reverse("shop_id")

    class Sale(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount: int = 0
        memo: str = ""
        shop_id: int = Field(foreign_key=Shop)

        shop: Relation[Shop] = Forward("shop_id")

    class LineItem(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        sale_id: int = Field(foreign_key=Sale)

        sale: Relation[Sale] = Forward("sale_id")

    resolve_relationships()
    return {"Shop": Shop, "Sale": Sale, "LineItem": LineItem}


def test_iterating_a_column_names_it_the_way_the_lambda_does(models):
    Sale = models["Sale"]
    with pytest.raises(TypeError) as caught:
        Sale.where(lambda sale: sum(sale.amount) > 10)
    message = str(caught.value)
    assert message.startswith("sale.amount is a column reference")
    assert "did you mean sale.amount.sum()?" in message


@pytest.mark.parametrize(
    "build, expected, names_having",
    [
        (
            lambda Sale: Sale.where(lambda sale: sale.amount.sum() > 10),
            "sale.amount.sum() > ...",
            True,
        ),
        (
            lambda Sale: Sale.where(lambda sale: sale.amount.sum()),
            "sale.amount.sum()",
            True,
        ),
        (
            lambda Sale: Sale.select(lambda sale: sale.amount.sum()),
            "sale.amount.sum()",
            False,
        ),
        (
            lambda Sale: Sale.select(lambda sale: {"x": sale.memo.sum()}),
            "sale.memo.sum()",
            False,
        ),
        (
            lambda Sale: Sale.select(lambda row: {"x": row.memo.sum()}),
            "row.memo.sum()",
            False,
        ),
    ],
)
def test_aggregate_misuse_names_the_lambda_parameter(
    models, build, expected, names_having
):
    with pytest.raises(TypeError) as caught:
        build(models["Sale"])
    message = str(caught.value)
    assert expected in message
    assert not _STALE.search(message), message
    if names_having:
        assert "Ferro has no having() yet" in message
        assert "compare the aggregated result in Python" in message


def test_a_string_selector_names_the_model_in_snake_case(models):
    with pytest.raises(TypeError) as caught:
        models["LineItem"].select().include("sale")
    assert "include(lambda line_item: line_item.sale)" in str(caught.value)


def test_a_partial_reads_its_first_remaining_parameter(models):
    Sale = models["Sale"]
    with pytest.raises(TypeError) as caught:
        Sale.where(functools.partial(lambda scale, sale: sum(sale.amount), 2))
    assert str(caught.value).startswith("sale.amount is a column reference")


def test_no_positional_parameter_falls_back_to_the_model_name(models):
    Sale = models["Sale"]
    with pytest.raises(TypeError) as caught:
        Sale.where(lambda *rows: sum(rows[0].amount))
    assert str(caught.value).startswith("sale.amount is a column reference")


def _big(sale: QueryProxy[Any]) -> QueryNode:
    return sale.amount > 10


def _columns(sale: QueryProxy[Any]) -> Any:
    return (sale.id, sale.amount)


def _amount(sale: QueryProxy[Any]) -> Any:
    return sale.amount


def _its_sale(line: QueryProxy[Any]) -> Any:
    return line.sale


def _bump(sale: QueryProxy[Any]) -> dict[str, Any]:
    return {"amount": sale.amount + 1}


def _iterates(sale: QueryProxy[Any]) -> QueryNode:
    return sum(sale.amount) > 10  # type: ignore[call-overload]  # ty: ignore[no-matching-overload]


@pytest.mark.asyncio
async def test_a_predicate_annotated_with_type_checking_names_builds(models, db_url):
    """Naming the row reads the parameter's name, never its annotation."""
    await connect(db_url, auto_migrate=True)
    Sale, LineItem = models["Sale"], models["LineItem"]
    Sale.where(_big)
    Sale.select(_columns)
    Sale.select().order_by(_amount)
    LineItem.select().include(_its_sale)
    LineItem.select().join(_its_sale)
    async with engines.session():
        assert await Sale.where(_big).update(_bump) == 0
    with pytest.raises(TypeError) as caught:
        Sale.where(_iterates)
    assert str(caught.value).startswith("sale.amount is a column reference")


@pytest.mark.asyncio
async def test_a_bare_reverse_relation_names_its_existence_test(models, db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError) as caught:
        models["Shop"].where(lambda shop: shop.sales)
    message = str(caught.value)
    assert "shop.sales.exists()" in message
    assert "~shop.sales.exists()" in message
    assert "ADR-" not in message


@pytest.mark.asyncio
async def test_an_inner_exists_lambda_keeps_its_own_name(models, db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError) as caught:
        models["Shop"].where(
            lambda shop: shop.sales.exists(lambda sale: sum(sale.amount) > 1)
        )
    assert str(caught.value).startswith("sale.amount is a column reference")


@pytest.mark.asyncio
async def test_an_inner_exists_message_names_the_outer_relation(models, db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError) as caught:
        models["Shop"].where(lambda shop: shop.sales.exists(lambda sale: sale.shop))
    message = str(caught.value)
    assert "shop.sales.exists(...)" in message
    assert "sale.shop.<column>" in message


def test_a_table_check_refusal_uses_the_check_lambda_name(clean_registry):
    class Owner(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str = ""

    with pytest.raises(TypeError) as caught:

        class Box(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (
                Check("owned", lambda box: box.owner.label == None),  # noqa: E711
            )

            id: Annotated[int | None, FerroField(primary_key=True)] = None
            owner_id: Annotated[int | None, FerroField(foreign_key=Owner)] = None

            owner: Relation[Owner | None] = Forward("owner_id")

    message = str(caught.value)
    assert "its foreign-key column (box.owner_id)" in message
    assert "ADR-" not in message


def _docstring_nodes(tree: ast.Module) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, owners) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def test_no_message_source_spells_t_or_cites_a_number():
    files = sorted((_SRC / "query").glob("*.py")) + [_SRC / "checks.py"]
    stale_lambda = re.compile(r"lambda t:|(?<![\w.])t\.[a-z_{<]")
    citation = re.compile(r"ADR-\d+|#\d+")
    hits: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            where = f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}"
            if stale_lambda.search(node.value):
                hits.append(f"{where}: {node.value!r}")
            elif id(node) not in docstrings and citation.search(node.value):
                hits.append(f"{where}: {node.value!r}")
    assert not hits, "\n".join(hits)
