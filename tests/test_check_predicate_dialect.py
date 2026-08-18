"""Full check-predicate dialect (#346): comparisons, in_, like, enum literals.

Transfer-shaped NULL/OR predicates stay byte-identical (#341); this file owns
the richer root-column ``where()`` dialect and its rejection rules (ADR-0016).
"""

import json
from enum import StrEnum
from typing import Annotated, ClassVar

import pytest

from ferro import (
    BackRef,
    Check,
    CheckViolationError,
    Field,
    ForeignKey,
    Model,
    Relation,
    clear_registry,
    connect,
    engines,
    reset_engine,
)
from ferro._core import _render_create_table_sql_for_test, _render_table_check_body
from ferro.ir.compiler import compile_registry_schema_ir


@pytest.fixture(autouse=True)
def cleanup_registry():
    from ferro.registry import REGISTRY

    def _wipe() -> None:
        reset_engine()
        clear_registry()
        REGISTRY.reset_for_test()

    _wipe()
    yield
    _wipe()


def _model_payload(table_name: str) -> dict:
    envelope = compile_registry_schema_ir()
    for model in envelope["payload"]["models"]:
        if model["table_name"] == table_name:
            return model
    raise AssertionError(f"{table_name} missing from the compiled modelset")


def _check_body(table_name: str, suffix: str) -> str:
    payload = _model_payload(table_name)
    entry = next(c for c in payload["table_checks"] if c["name"] == f"ck_{table_name}_{suffix}")
    return _render_table_check_body(json.dumps(entry["predicate"]))


# ---------------------------------------------------------------------------
# Compilation — comparisons, in_, like, column-to-column
# ---------------------------------------------------------------------------


def test_literal_comparison_compiles_to_cmp_ir():
    class Ledger(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("non_negative", lambda ledger: ledger.amount >= 0),
        )

        id: int | None = Field(default=None, primary_key=True)
        amount: int = 0

    predicate = _model_payload("ledger")["table_checks"][0]["predicate"]
    assert predicate == {
        "kind": "cmp",
        "column": "amount",
        "op": "ge",
        "other": {"kind": "literal", "token": "0"},
    }
    assert _check_body("ledger", "non_negative") == '"amount" >= 0'


def test_column_to_column_comparison_compiles():
    class Window(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("ordered", lambda window: window.end > window.start),
        )

        id: int | None = Field(default=None, primary_key=True)
        start: int = 0
        end: int = 0

    predicate = _model_payload("window")["table_checks"][0]["predicate"]
    assert predicate == {
        "kind": "cmp",
        "column": "end",
        "op": "gt",
        "other": {"kind": "column", "name": "start"},
    }
    assert _check_body("window", "ordered") == '"end" > "start"'


def test_in_and_like_compile_with_quoted_literal_tokens():
    class Memo(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("allowed_codes", lambda memo: memo.code.in_(("A", "B"))),
            Check("prefix", lambda memo: memo.label.like("draft%")),
        )

        id: int | None = Field(default=None, primary_key=True)
        code: str = ""
        label: str = ""

    checks = _model_payload("memo")["table_checks"]
    assert checks[0]["predicate"] == {
        "kind": "in",
        "column": "code",
        "values": ["'A'", "'B'"],
    }
    assert checks[1]["predicate"] == {
        "kind": "like",
        "column": "label",
        "pattern": "'draft%'",
    }


def test_enum_member_inlines_as_label_not_bind():
    class Status(StrEnum):
        DRAFT = "draft"
        ACTIVE = "active"

    class Ticket(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("draft_only", lambda ticket: ticket.status == Status.DRAFT),
        )

        id: int | None = Field(default=None, primary_key=True)
        status: Status = Status.DRAFT

    predicate = _model_payload("ticket")["table_checks"][0]["predicate"]
    assert predicate == {
        "kind": "cmp",
        "column": "status",
        "op": "eq",
        "other": {"kind": "literal", "token": "'draft'"},
    }
    body = _check_body("ticket", "draft_only")
    assert body == '"status" = \'draft\''
    assert "?" not in body
    assert "$" not in body


# ---------------------------------------------------------------------------
# Rejections — closures, calls, traversal, exists, aggregates
# ---------------------------------------------------------------------------


def test_closed_over_variable_fails_at_class_definition():
    min_amount = 0

    with pytest.raises(TypeError, match="closes over"):

        class BadClosure(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (
                Check("positive", lambda bad: bad.amount > min_amount),
            )

            id: int | None = Field(default=None, primary_key=True)
            amount: int = 0


def test_function_call_in_predicate_fails_at_class_definition():
    with pytest.raises(TypeError, match="function call"):

        class BadCall(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (
                Check("abs_positive", lambda bad: bad.amount > abs(5)),
            )

            id: int | None = Field(default=None, primary_key=True)
            amount: int = 0


@pytest.mark.parametrize(
    "predicate, expected",
    [
        (lambda t: t.owner.label == None, "traversal"),  # noqa: E711
        (lambda t: t.notes.exists(), "existence test"),
        (lambda t: t.amount.sum(), "aggregate"),
    ],
)
def test_still_rejected_predicate_forms(predicate, expected):
    class RejectOwner(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str | None = None
        rejects: Relation[list["Rejecting"]] = BackRef()

    with pytest.raises(TypeError, match=expected):

        class Rejecting(Model):
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (Check("rule", predicate),)

            id: int | None = Field(default=None, primary_key=True)
            amount: int = 0
            owner: Annotated[RejectOwner | None, ForeignKey(related_name="rejects")] = None
            notes: Relation[list["RejectNote"]] = BackRef()

        class RejectNote(Model):
            id: int | None = Field(default=None, primary_key=True)
            rejecting: Annotated[Rejecting, ForeignKey(related_name="notes")]


# ---------------------------------------------------------------------------
# Live enforcement (backend_matrix)
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_non_negative_amount_check_enforced_live(db_url):
    class Balance(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("non_negative", lambda balance: balance.amount >= 0),
        )

        id: int | None = Field(default=None, primary_key=True)
        amount: int = 0

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Balance.create(amount=10)
        with pytest.raises(CheckViolationError):
            await Balance.create(amount=-1)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_column_to_column_check_enforced_live(db_url):
    class Interval(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("ordered", lambda interval: interval.end > interval.start),
        )

        id: int | None = Field(default=None, primary_key=True)
        start: int = 0
        end: int = 0

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Interval.create(start=1, end=5)
        with pytest.raises(CheckViolationError):
            await Interval.create(start=5, end=1)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_in_check_enforced_live(db_url):
    class Code(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("allowed", lambda code: code.value.in_(("A", "B"))),
        )

        id: int | None = Field(default=None, primary_key=True)
        value: str = ""

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Code.create(value="A")
        with pytest.raises(CheckViolationError):
            await Code.create(value="Z")
