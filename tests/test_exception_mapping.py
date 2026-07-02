"""Database failures raised from the Rust core are catchable by type (FF-A / A2, #172).

Exit-gate rule: exception *types* (and structured attributes) are asserted —
never driver message text.
"""

from typing import Annotated

import pytest

from ferro import (
    BackRef,
    FerroField,
    ForeignKey,
    InterfaceError,
    IntegrityError,
    Model,
    NotNullViolationError,
    OperationalError,
    Relation,
    UniqueViolationError,
    connect,
)
from ferro import Field
from ferro.exceptions import CheckViolationError, ForeignKeyViolationError


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_unique_violation_is_typed(db_url, db_backend):
    class UniqueUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]

    await connect(db_url, auto_migrate=True)
    await UniqueUser(email="a@b.com").save()

    with pytest.raises(UniqueViolationError) as excinfo:
        await UniqueUser(email="a@b.com").save()

    exc = excinfo.value
    assert isinstance(exc, IntegrityError)
    assert exc.driver_message is not None
    if db_backend == "postgres":
        assert exc.sqlstate == "23505"
        assert exc.constraint is not None


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_not_null_violation_is_typed(db_url, db_backend):
    class NotNullNote(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        body: str

    await connect(db_url, auto_migrate=True)
    note = NotNullNote(body="text")
    await note.save()
    pk = note.id

    with pytest.raises(NotNullViolationError) as excinfo:
        await NotNullNote.where(lambda note: note.id == pk).update(body=None)

    exc = excinfo.value
    assert isinstance(exc, IntegrityError)
    assert exc.driver_message is not None
    if db_backend == "postgres":
        assert exc.sqlstate == "23502"


# db_check constraints are emitted on Postgres only (SQLite would need a
# table rebuild; ferro-ddl-lowering elides them there with a warning), so a
# CheckViolationError can only originate on Postgres.
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_check_violation_is_typed(db_url, db_backend):
    import enum

    class DocFormat(str, enum.Enum):
        MARKDOWN = "markdown"
        HTML = "html"

    class CheckedDoc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        format: Annotated[DocFormat, Field(db_type="text", db_check=True)]

    await connect(db_url, auto_migrate=True)
    doc = CheckedDoc(format=DocFormat.MARKDOWN)
    await doc.save()
    pk = doc.id

    # Pydantic validation is bypassed on the bulk-update path by design;
    # the DB CHECK constraint is the enforcement of record.
    with pytest.raises(CheckViolationError) as excinfo:
        await CheckedDoc.where(lambda doc: doc.id == pk).update(format="bogus")

    exc = excinfo.value
    assert isinstance(exc, IntegrityError)
    if db_backend == "postgres":
        assert exc.sqlstate == "23514"


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_foreign_key_violation_is_typed(db_url, db_backend):
    class FkAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        books: Relation[list["FkBook"]] = BackRef()

    class FkBook(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[FkAuthor, ForeignKey(related_name="books")]

    await connect(db_url, auto_migrate=True)

    with pytest.raises(ForeignKeyViolationError) as excinfo:
        await FkBook(title="Ghost", author_id=99999).save()

    exc = excinfo.value
    assert isinstance(exc, IntegrityError)
    if db_backend == "postgres":
        assert exc.sqlstate == "23503"


@pytest.mark.asyncio
async def test_unopenable_database_is_operational_error(tmp_path):
    # No mode=rwc: SQLite refuses to open a missing file, which surfaces as a
    # connect-time database error.
    with pytest.raises(OperationalError):
        await connect(f"sqlite://{tmp_path}/missing/nope.db", auto_migrate=False)


@pytest.mark.asyncio
async def test_unsupported_scheme_is_interface_error():
    with pytest.raises(InterfaceError):
        await connect("mysql://root@localhost/nope", auto_migrate=False)


@pytest.mark.asyncio
async def test_unknown_connection_name_is_interface_error(tmp_path):
    class RoutedItem(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(f"sqlite://{tmp_path}/routed.db?mode=rwc", auto_migrate=True)

    from ferro.query.builder import Query

    with pytest.raises(InterfaceError):
        await Query(RoutedItem, using="no_such_connection").all()
