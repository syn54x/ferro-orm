"""Retired enum labels (ADR-0053, #627).

``__ferro_retired_labels__ = frozenset({"canceled"})`` on a ``StrEnum`` stops
this release writing the label while the member, the database type and any
check keep it, so the previous release's writes stay valid. Names start with
``Rtl`` so this module never shares a type name with another suite on the same
Postgres server.
"""

from enum import Enum, StrEnum
from typing import Annotated

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from ferro import Field, FerroField, Model, connect, engines
from ferro.ir.compiler import compile_model_schema_ir, declared_retired_labels
from tests.test_generate_enum_removal import (
    ALL,
    KEPT,
    SWAP,
    checked_models,
    labels_of,
    models,
    start,
    statuses,
    text,
    write_value,
)
from tests.test_generate_enum_removal import (  # noqa: F401 - fixtures
    keys,
    listing,
    migration_dir,
    no_bytecode,
    pkg,
    project,
    run,
    write_config,
    write_models,
    db,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

RETIRED = '    __ferro_retired_labels__ = frozenset({"canceled"})\n'
MESSAGE = (
    "RtlStatus.CANCELED ('canceled') is retired in this release: the previous "
    "release may still write it, this one does not"
)


class RtlStatus(StrEnum):
    __ferro_retired_labels__ = frozenset({"canceled"})
    PAID = "paid"
    CANCELED = "canceled"


class RtlOrder(Model):
    id: int | None = Field(default=None, primary_key=True)
    status: RtlStatus = RtlStatus.PAID
    note: str = ""


class RtlOrderAnnotated(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    status: Annotated[RtlStatus, FerroField()] = RtlStatus.PAID
    note: str = ""


# -- the declaration ------------------------------------------------------------------


def test_the_declaration_is_read_as_a_frozenset_of_member_labels():
    assert declared_retired_labels(RtlStatus) == frozenset({"canceled"})

    class Plain(StrEnum):
        A = "a"

    assert declared_retired_labels(Plain) == frozenset()


class RtlBad(StrEnum):
    __ferro_retired_labels__ = frozenset({"cancelled"})
    PAID = "paid"
    CANCELED = "canceled"


def test_a_retired_label_naming_no_member_is_a_type_error_naming_the_enum():
    with pytest.raises(TypeError) as raised:

        class RtlBadOrder(Model):
            id: int | None = Field(default=None, primary_key=True)
            status: RtlBad

    message = str(raised.value)
    assert "RtlBad.__ferro_retired_labels__" in message
    assert "cancelled" in message and "['paid', 'canceled']" in message
    assert "delete the member and this declaration together" in message


@pytest.mark.parametrize("declared", ["canceled", {"canceled": 1}, [1], 3])
def test_any_other_shape_is_a_type_error_naming_the_fix(declared):
    class RtlShape(StrEnum):
        PAID = "paid"
        CANCELED = "canceled"

    RtlShape.__ferro_retired_labels__ = declared
    with pytest.raises(TypeError, match="must be a frozenset of label strings"):
        declared_retired_labels(RtlShape)


def test_a_plain_set_is_accepted():
    class RtlSet(StrEnum):
        __ferro_retired_labels__ = {"canceled"}
        PAID = "paid"
        CANCELED = "canceled"

    assert declared_retired_labels(RtlSet) == frozenset({"canceled"})


# -- the write is refused -------------------------------------------------------------


@pytest.mark.parametrize("model", [RtlOrder, RtlOrderAnnotated])
def test_construction_with_the_retired_label_is_a_validation_error(model):
    for value in ("canceled", RtlStatus.CANCELED):
        with pytest.raises(ValidationError) as raised:
            model(status=value)
        assert MESSAGE in str(raised.value)
    assert model(status="paid").status is RtlStatus.PAID


@pytest.mark.parametrize("model", [RtlOrder, RtlOrderAnnotated])
def test_attribute_assignment_of_the_retired_label_is_a_validation_error(model):
    order = model()
    with pytest.raises(ValidationError) as raised:
        order.status = RtlStatus.CANCELED
    assert MESSAGE in str(raised.value)
    with pytest.raises(ValidationError):
        order.status = "canceled"
    assert order.status is RtlStatus.PAID
    order.note = "still assignable"
    order.status = RtlStatus.PAID


def test_a_hydrated_row_holding_the_label_is_not_validated():
    order = RtlOrder.model_construct(id=1, status=RtlStatus.CANCELED, note="")
    assert order.status is RtlStatus.CANCELED
    order.note = "changed"
    assert order.status is RtlStatus.CANCELED


class RtlLive(StrEnum):
    PAID = "paid"


class RtlLiveOrder(Model):
    id: int | None = Field(default=None, primary_key=True)
    status: RtlLive


# -- the snapshot carries the declaration ---------------------------------------------


def _status_column(model) -> dict:
    envelope = compile_model_schema_ir(model.__ferro_identity__, model)
    return next(c for c in envelope["payload"]["models"][0]["columns"] if c["name"] == "status")


def test_the_column_carries_the_retired_labels_and_an_undeclared_enum_does_not():
    assert _status_column(RtlOrder)["enum_retired_labels"] == ["canceled"]

    assert "enum_retired_labels" not in _status_column(RtlLiveOrder)
    assert _status_column(RtlLiveOrder)["enum_values"] == ["paid"]


# -- a hydrated row is saved back -----------------------------------------------------


def order_model(validate_assignment: bool = False) -> type[Model]:
    """A fresh ``RtlOrder`` registered for this test (the registry is wiped per test)."""

    class RtlOrder(Model):
        model_config = ConfigDict(validate_assignment=validate_assignment)
        id: int | None = Field(default=None, primary_key=True)
        status: RtlStatus = RtlStatus.PAID
        note: str = ""

    return RtlOrder


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_row_holding_the_retired_label_hydrates_and_saves_after_another_change(
    db_url,
):
    RtlOrder = order_model()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await RtlOrder.model_construct(
            id=1, status=RtlStatus.CANCELED, note="a"
        ).save()

        order = await RtlOrder.where(lambda rtlorder: rtlorder.id == 1).first()
        assert order is not None and order.status is RtlStatus.CANCELED
        order.note = "b"
        await order.save()

        again = await RtlOrder.where(lambda rtlorder: rtlorder.id == 1).first()
        assert again is not None
        assert (again.status, again.note) == (RtlStatus.CANCELED, "b")


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_bulk_create_of_live_labels_beside_a_hydrated_retired_row(db_url):
    RtlOrder = order_model()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await RtlOrder.model_construct(
            id=1, status=RtlStatus.CANCELED, note="a"
        ).save()
        held = await RtlOrder.where(lambda rtlorder: rtlorder.id == 1).first()
        assert held is not None

        await RtlOrder.bulk_create(
            [RtlOrder(id=2, status=RtlStatus.PAID), RtlOrder(id=3, note="x")]
        )

        rows = await RtlOrder.select().order_by(lambda rtlorder: rtlorder.id).all()
        assert [(r.id, r.status) for r in rows] == [
            (1, RtlStatus.CANCELED),
            (2, RtlStatus.PAID),
            (3, RtlStatus.PAID),
        ]


# -- retiring a label renders nothing -------------------------------------------------


def retire(body: str) -> str:
    return body.replace("(StrEnum):\n", "(StrEnum):\n" + RETIRED, 1)


@pytest.mark.backend_matrix
def test_retiring_a_label_is_no_schema_change_and_the_type_keeps_it(
    project, pkg, db, capsys
):
    start(project, pkg, db, models(ALL))
    before = listing(project / "migrations")
    write_models(project, pkg, retire(models(ALL)))
    capsys.readouterr()

    assert run("migrate", "new", "retire_canceled") == 0

    assert capsys.readouterr().out == "no schema change: nothing written\n"
    assert listing(project / "migrations") == before
    assert run("migrate", "check") == 0
    if db.backend == "postgres":
        assert labels_of(db) == list(ALL)


@pytest.mark.backend_matrix
def test_retiring_a_label_leaves_the_db_check_listing_it(project, pkg, db, capsys):
    start(project, pkg, db, checked_models(ALL))
    before = listing(project / "migrations")
    write_models(project, pkg, retire(checked_models(ALL)))
    capsys.readouterr()

    assert run("migrate", "new", "retire_canceled") == 0

    assert capsys.readouterr().out == "no schema change: nothing written\n"
    assert listing(project / "migrations") == before
    assert run("migrate", "check") == 0
    if db.backend == "postgres":
        body = db.rows(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_rmlorder_status' AND conrelid = "
            "'rmlorder'::regclass"
        )[0][0]
        assert "canceled" in body


@pytest.mark.backend_matrix
def test_deleting_the_member_and_the_declaration_is_the_existing_removal(
    project, pkg, db, capsys
):
    start(project, pkg, db, retire(models(ALL)))
    db.execute("INSERT INTO \"rmlorder\" (\"status\") VALUES ('canceled')")
    write_models(project, pkg, models(KEPT))
    capsys.readouterr()

    new("drop_canceled")

    assert "removed enum labels: rmlorderstatus.canceled" in capsys.readouterr().out
    migration = migration_dir(project, 2)
    write_value(migration, "01_backfill_rmlorder.py", '"refunded"')
    assert run("migrate", "up", "--url", db.url) == 0
    assert statuses(db) == [(1, "refunded")]
    if db.backend == "postgres":
        assert labels_of(db) == list(KEPT)
        assert text(migration, "02_contract.up.postgres.sql") == (
            "-- ferro: data-dependent\n\n" + "\n\n".join(f"{s};" for s in SWAP) + "\n"
        )


class RtlPlain(Model):
    id: int | None = Field(default=None, primary_key=True)
    status: RtlLive


def test_a_model_without_retired_labels_gains_no_setattr_or_validator():
    assert RtlPlain.__setattr__ is Model.__setattr__
    assert RtlPlain.model_fields["status"].metadata == []
    assert Model.__setattr__ is BaseModel.__setattr__


@pytest.mark.parametrize("validate_assignment", [False, True])
def test_assigning_the_label_fails_with_the_ticket_text_under_both_configs(
    validate_assignment,
):
    order = order_model(validate_assignment)()
    with pytest.raises(ValidationError) as raised:
        order.status = RtlStatus.CANCELED
    assert "RtlStatus.CANCELED ('canceled') is retired in this release" in str(
        raised.value
    )
    with pytest.raises(ValidationError):
        order_model(validate_assignment)(status="canceled")


@pytest.mark.backend_matrix
@pytest.mark.asyncio
@pytest.mark.parametrize("validate_assignment", [False, True])
async def test_a_hydrated_retired_row_changes_another_field_and_saves(
    db_url, validate_assignment
):
    RtlOrder = order_model(validate_assignment)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await RtlOrder.model_construct(
            id=1, status=RtlStatus.CANCELED, note="a"
        ).save()
        order = await RtlOrder.where(lambda rtlorder: rtlorder.id == 1).first()
        assert order is not None and order.status is RtlStatus.CANCELED

        order.note = "b"
        await order.save()

        again = await RtlOrder.where(lambda rtlorder: rtlorder.id == 1).first()
        assert again is not None
        assert (again.status, again.note) == (RtlStatus.CANCELED, "b")


# -- a plain Enum is guarded by its member values (panel F4) --------------------------


class RtlPlainStatus(Enum):
    __ferro_retired_labels__ = frozenset({"canceled"})
    PAID = "paid"
    CANCELED = "canceled"


PLAIN_MESSAGE = (
    "RtlPlainStatus.CANCELED ('canceled') is retired in this release: the previous "
    "release may still write it, this one does not"
)


def plain_order_model(validate_assignment: bool = False) -> type[Model]:
    """An order whose status is a plain ``Enum`` (not a ``str`` subclass)."""

    class RtlPlainOrder(Model):
        model_config = ConfigDict(validate_assignment=validate_assignment)
        id: int | None = Field(default=None, primary_key=True)
        status: RtlPlainStatus = RtlPlainStatus.PAID
        note: str = ""

    return RtlPlainOrder


@pytest.mark.parametrize("validate_assignment", [False, True])
def test_a_plain_enum_refuses_the_retired_label_on_construction(validate_assignment):
    RtlPlainOrder = plain_order_model(validate_assignment)
    for value in ("canceled", RtlPlainStatus.CANCELED):
        with pytest.raises(ValidationError) as raised:
            RtlPlainOrder(status=value)
        assert PLAIN_MESSAGE in str(raised.value)
    assert RtlPlainOrder(status="paid").status is RtlPlainStatus.PAID


@pytest.mark.parametrize("validate_assignment", [False, True])
def test_a_plain_enum_refuses_the_retired_label_on_assignment(validate_assignment):
    order = plain_order_model(validate_assignment)()
    for value in (RtlPlainStatus.CANCELED, "canceled"):
        with pytest.raises(ValidationError) as raised:
            order.status = value
        assert PLAIN_MESSAGE in str(raised.value)
    assert order.status is RtlPlainStatus.PAID
    order.status = RtlPlainStatus.PAID
    order.note = "still assignable"


@pytest.mark.backend_matrix
@pytest.mark.asyncio
@pytest.mark.parametrize("validate_assignment", [False, True])
async def test_a_hydrated_plain_enum_retired_row_changes_another_field_and_saves(
    db_url, validate_assignment
):
    RtlPlainOrder = plain_order_model(validate_assignment)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await RtlPlainOrder.model_construct(
            id=1, status=RtlPlainStatus.CANCELED, note="a"
        ).save()
        order = await RtlPlainOrder.where(
            lambda rtlplainorder: rtlplainorder.id == 1
        ).first()
        assert order is not None and order.status is RtlPlainStatus.CANCELED

        order.note = "b"
        await order.save()

        again = await RtlPlainOrder.where(
            lambda rtlplainorder: rtlplainorder.id == 1
        ).first()
        assert again is not None
        assert (again.status, again.note) == (RtlPlainStatus.CANCELED, "b")
