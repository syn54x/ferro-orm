"""Define the core ORM model base and transaction helpers for Ferro."""

from contextlib import asynccontextmanager
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Self,
    overload,
)

if TYPE_CHECKING:
    from .checks import Check
    from .columns import ColumnSpec, RelationSpec, ReverseSpec
    from .query import Predicate, ProjectedQuery, RowSelector
    from .rowsecurity import RowSecurity
    from .session import Session

from pydantic import BaseModel, ConfigDict, model_validator

from ._bind_payload import (
    apply_save_exclude,
    apply_save_only,
    normalize_save_columns,
    save_bind_payload,
)
from ._core import (
    begin_transaction,
    commit_transaction,
    fetch_all,
    register_instance,
    rollback_transaction,
    save_bulk_records,
    save_record,
    transaction_connection_name,
    update_record,
)
from ._core import (
    evict_instance as _core_evict_instance,
)
from .base import ForeignKey, foreign_key_allows_none
from .exceptions import ModelDoesNotExist
from .metaclass import ModelMetaclass
from .query import Predicate, Query
from .state import (
    _CURRENT_TRANSACTION,
    _CURRENT_TRANSACTION_CONNECTION,
    RouteHandle,
    resolve_operation_scope,
    resolve_transaction_scope,
    route_for_transaction,
)

_FERRO_CONNECTION_ATTR = "__ferro_connection_name"
_FERRO_PERSISTED_ATTR = "__ferro_persisted"


def _is_persisted(instance: object) -> bool:
    """True when the instance is known to have a row in the database.

    Set by Rust hydration (`hydrate_model_instance`) and by a successful
    ``save()``; cleared by ``delete()``. Absent means transient (FF-A A4).
    """
    return bool(instance.__dict__.get(_FERRO_PERSISTED_ATTR, False))


def _set_persisted(instance: object, value: bool) -> None:
    object.__setattr__(instance, _FERRO_PERSISTED_ATTR, value)


def _field_eq(field_name: str, value: Any) -> Predicate[Any]:
    """Build a lambda predicate for dynamic field equality without operator style."""
    return lambda t, f=field_name, v=value: getattr(t, f) == v


async def _transaction_or_using(
    using: str | None, session: "Session | None"
) -> RouteHandle:
    from . import _ensure_rust_registration_synced_for_operation

    await _ensure_rust_registration_synced_for_operation()
    return resolve_operation_scope(using=using, session=session)


async def _instance_transaction_route(
    instance: object, using: str | None, session: "Session | None"
) -> tuple[RouteHandle, str]:
    """Resolve the route for an instance method (`save`/`delete`/`refresh`).

    Returns `(route, effective_connection)`. `effective_connection` is
    `route.connection_name` — always the real connection the instance's
    identity-map entry lives on, whether or not an ambient transaction is
    active (FF-D D3/D4 folded the old `identity_using`/`operation_using`
    split into the single resolved route).
    """
    origin = _instance_origin(instance)
    if using is not None and origin is not None and using != origin:
        raise ValueError("Instance is already bound to a different connection")

    # Origin is the instance's implicit route (FF-D D4): it participates in
    # session-conflict checks instead of silently bypassing the session.
    route = await _transaction_or_using(using or origin, session)
    return route, route.connection_name


def _instance_origin(instance: object) -> str | None:
    origin = getattr(instance, _FERRO_CONNECTION_ATTR, None)
    return origin if isinstance(origin, str) else None


def _set_instance_origin(instance: object, using: str | None) -> None:
    if using is not None:
        object.__setattr__(instance, _FERRO_CONNECTION_ATTR, using)


def evict_instance(
    model: "type[Model] | str",
    pk: str,
    *,
    using: str | None = None,
    session: "Session | None" = None,
) -> None:
    """Remove one instance from the session's identity map.

    Within a session, fetching a row you already hold returns the same
    object. After an eviction, the next fetch of that row builds a new
    instance and leaves the one you hold detached. A re-fetch already
    refreshes a held instance in place, so evict only when you want a second,
    separate object. See
    [Eviction](../concepts/identity-map.md#eviction).

    Args:
        model: The model class, or its name when the name is unambiguous.
        pk: The instance's primary key, as a string.
        using: The named connection whose map to evict from. Defaults to
            `None`, the session's connection.
        session: The session whose map to evict from. Defaults to `None`, the
            session opened by the enclosing `async with engines.session():`.

    Raises:
        RuntimeError: No session, `using=` or transaction gives the call a
            database route.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import evict_instance

        ada = await Author.get(1)
        evict_instance(Author, "1")
        fresh = await Author.get(1)
        assert fresh is not ada
        ```
    """
    # The instance methods call the core's evict with the route they already
    # resolved; this wrapper resolves it once here.
    from .registry import REGISTRY

    model_cls = REGISTRY.resolve_reference(model) if isinstance(model, str) else model
    route = resolve_operation_scope(using=using, session=session)
    _core_evict_instance(model_cls.__ferro_identity__, pk, route)


@asynccontextmanager
async def transaction(
    using: str | None = None,
    *,
    session: "Session | None" = None,
    immediate: bool = False,
):
    """Run the operations in the block in one database transaction.

    The transaction commits when the block exits and rolls back when it
    raises. A `transaction()` inside another one opens a savepoint. The
    yielded [`Transaction`][ferro.Transaction] handle runs raw SQL on the
    transaction's connection, for statements no model covers, such as
    setting a Postgres setting or taking an advisory lock. See
    [Transactions](../guide/transactions.md).

    Args:
        using: The named connection to open the transaction on. Defaults to
            `None`, the session's connection.
        session: The session to open the transaction in. Defaults to `None`,
            the session opened by the enclosing `async with engines.session():`.
        immediate: On SQLite, open with `BEGIN IMMEDIATE`, which takes the
            write lock at once, so a transaction that reads and then writes
            never fails with `SQLITE_BUSY` while upgrading its lock. Postgres
            and nested transactions ignore it. Defaults to `False`.

    Yields:
        tx (ferro.Transaction): A handle with `execute`, `fetch_all` and
            `fetch_one` for raw SQL on the transaction's connection.

    Raises:
        RuntimeError: No session, `using=` or transaction gives the call a
            database route.

    Examples:
        A failure inside the block rolls back every write in it:

        ```python {test=run prelude=reference}
        from ferro import transaction

        try:
            async with transaction():
                await Author.create(name="Mary Somerville", email="mary@example.com")
                raise RuntimeError("abandon the import")
        except RuntimeError:
            pass

        assert await Author.where(lambda author: author.name == "Mary Somerville").count() == 0
        ```

        The handle runs raw SQL in the same transaction:

        ```python {test=run prelude=reference}
        from ferro import transaction

        async with transaction() as tx:
            await Author.create(name="Mary Somerville", email="mary@example.com")
            row = await tx.fetch_one("SELECT count(*) AS total FROM author")

        assert row == {"total": 3}
        ```
    """
    from .raw import Transaction

    route = resolve_transaction_scope(using=using, session=session)
    tx_id = await begin_transaction(route, immediate)
    connection_name = transaction_connection_name(tx_id, session_id=route.session_id)
    child_route = route_for_transaction(connection_name, tx_id, route.session_id)
    token = _CURRENT_TRANSACTION.set(tx_id)
    connection_token = _CURRENT_TRANSACTION_CONNECTION.set(connection_name)
    try:
        yield Transaction(child_route)
        await commit_transaction(tx_id, session_id=route.session_id)
    except Exception:
        await rollback_transaction(tx_id, session_id=route.session_id)
        raise
    except BaseException as interruption:
        # Cancellation and KeyboardInterrupt skip `except Exception`; without
        # this the registry would keep the pinned connection forever. The
        # interruption is what the caller must see, so a failing rollback is
        # attached to it as ``__context__`` rather than replacing it.
        try:
            await rollback_transaction(tx_id, session_id=route.session_id)
        except BaseException as rollback_failure:
            rollback_failure.__context__ = None
            interruption.__context__ = rollback_failure
        raise
    finally:
        _CURRENT_TRANSACTION.reset(token)
        _CURRENT_TRANSACTION_CONNECTION.reset(connection_token)


class Model(BaseModel, metaclass=ModelMetaclass):
    """The base class of every Ferro model: a Pydantic model backed by a table.

    A subclass declares its columns as annotated fields, and its table is
    created on [`connect`][ferro.connect] with `auto_migrate=True`. Every
    database call runs inside a session, opened with
    `async with engines.session():`, and the examples on this page run in
    one. See [Models and fields](../guide/models-and-fields.md) to declare a
    model and [Connections](../guide/connections.md#sessions) for
    sessions.

    A model declares table-wide constraints in class attributes:
    `__ferro_composite_uniques__` and `__ferro_composite_indexes__` (see
    [Composite constraints](../guide/models-and-fields.md#composite-constraints)),
    `__ferro_checks__` (see
    [Table checks](../guide/models-and-fields.md#table-checks)) and
    `__ferro_rls__` (see [Using with Postgres RLS](../guide/row-level-security.md)).

    Examples:
        ```python {test=run prelude=reference}
        mary = await Author.create(name="Mary Somerville", email="mary@example.com")
        fetched = await Author.get(mary.id)
        assert fetched is mary
        ```

        Within a session, fetching a row you already hold returns the same
        object.
    """

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    #: Table-level CHECK constraints (ADR-0012); an empty tuple is a no-op.
    __ferro_checks__: ClassVar[tuple["Check", ...]] = ()
    #: Row security declaration (PRD #406); ``None`` leaves the table unpoliced.
    __ferro_rls__: ClassVar["RowSecurity | None"] = None
    __ferro_columns__: ClassVar[dict[str, "ColumnSpec"]] = {}
    #: The single PK field name, derived once at the compile choke point
    #: alongside ``__ferro_columns__``; ``None`` for a PK-less model.
    __ferro_pk__: ClassVar[str | None] = None
    __ferro_relation_specs__: ClassVar[dict[str, "RelationSpec"]] = {}
    #: Reverse (BackRef) relation facts for existence tests (#314, ADR-0007),
    #: refreshed at the same compile choke point as ``__ferro_relation_specs__``.
    __ferro_reverse_specs__: ClassVar[dict[str, "ReverseSpec"]] = {}
    #: Retired columns (ADR-0053), refreshed at the same compile choke point:
    #: in the schema snapshot, never in ``__ferro_columns__``.
    __ferro_retired_columns__: ClassVar[dict[str, "ColumnSpec"]] = {}
    #: Every name a retired declaration took out of the model: retired fields,
    #: retired relations and their shadow ``*_id`` columns.
    __ferro_retired_names__: ClassVar[frozenset[str]] = frozenset()
    _enum_fields: ClassVar[dict[str, type[Enum]]] = {}

    @classmethod
    def _reregister_ferro(cls) -> None:
        """Re-persist this model's SchemaIR envelope (e.g. after envelope eviction)."""
        from .ir.compiler import compile_model_schema_ir

        compile_model_schema_ir(cls.__ferro_identity__, cls)

    model_config = ConfigDict(
        from_attributes=True,
        use_attribute_docstrings=True,
        arbitrary_types_allowed=True,
    )
    """Pydantic settings shared by every model; a subclass may extend them."""

    def __init__(self, **data: Any):
        """Build an instance in memory, without saving it.

        Args:
            **data: The field values. A relation field takes the related
                instance or its primary key.

        Raises:
            TypeError: A value names a retired field.
            ValueError: A related instance's model has no primary key.
        """
        retired = self.__class__.__ferro_retired_names__
        if retired and not retired.isdisjoint(data):
            name = min(retired.intersection(data))
            raise TypeError(
                f"{self.__class__.__name__}.{name} is retired in this release: the "
                f"model no longer reads or writes it (ADR-0053); drop {name}= from "
                "the call"
            )
        # 1. Handle relationship inputs (e.g. Product(category=my_cat))
        relations = getattr(self.__class__, "ferro_relations", {})
        for field_name, metadata in relations.items():
            if isinstance(metadata, ForeignKey) and field_name in data:
                val = data.pop(field_name)
                # If it's a Model instance, extract the ID
                if isinstance(val, Model):
                    # Read the *target* model's PK (FF-D D5) — the source
                    # model's PK name is irrelevant to the related instance.
                    pk_field = val.__class__.__ferro_pk__
                    if pk_field is None:
                        raise ValueError(
                            f"Cannot assign a {val.__class__.__name__} instance "
                            f"to relationship field {field_name!r}: "
                            f"{val.__class__.__name__} declares no primary-key "
                            "column, so there is no value to store in "
                            f"{field_name!r}_id. Pass the scalar value directly "
                            "or declare a primary_key=True column."
                        )
                    data[f"{field_name}_id"] = getattr(val, pk_field, None)
                else:
                    # It's already an ID or something else
                    data[f"{field_name}_id"] = val

        super().__init__(**data)

    if not TYPE_CHECKING:

        def __getattr__(self, item: str) -> Any:
            # Only a miss reaches here: a retired field (ADR-0053) is no
            # attribute, and the error says it is gone on purpose.
            if item in type(self).__ferro_retired_names__:
                raise AttributeError(
                    f"{type(self).__name__}.{item} is retired in this release: the "
                    "model no longer reads or writes it (ADR-0053)",
                    name=item,
                    obj=self,
                )
            return super().__getattr__(item)

    @model_validator(mode="after")
    def _validate_required_foreign_keys(self) -> Self:
        """Keep Python model validation aligned with required FK nullability."""
        relations = getattr(self.__class__, "ferro_relations", {})
        for field_name, metadata in relations.items():
            if not isinstance(metadata, ForeignKey):
                continue
            if foreign_key_allows_none(metadata) is False:
                if getattr(self, f"{field_name}_id", None) is None:
                    raise ValueError(f"{field_name} is required")
        return self

    async def save(
        self,
        *,
        using: str | None = None,
        session: "Session | None" = None,
        on_conflict: Literal["update"] | None = None,
        only: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
        exclude: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Write the instance to its row: insert it when new, update it otherwise.

        An instance you built and never saved is inserted. One fetched from
        the database, or saved before, updates its row by primary key, and
        `only=` or `exclude=` limit that update to some columns. For an
        update across many rows, use [`Query.update`][ferro.query.builder.Query.update].
        See [Saving: INSERT or UPDATE](../guide/mutations.md#saving-insert-or-update).

        Args:
            using: The named connection to write on. Defaults to `None`, the
                session's connection, or the one the instance was loaded from.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.
            on_conflict: `"update"` inserts the row or, when its primary key
                already exists, updates it, whether or not the instance was
                saved before. Defaults to `None`, a plain insert or update.
            only: On an update, the columns to write, as a set, list or
                tuple of field names. Defaults to `None`, every column.
            exclude: On an update, the columns not to write. Defaults to
                `None`; `exclude=set()` writes every column.

        Raises:
            UniqueViolationError: An insert repeats a primary key or unique
                value.
            ModelDoesNotExist: The row behind a saved instance no longer
                exists, or its primary key was changed in memory.
            TypeError: `only` or `exclude` is a bare string.
            ValueError: `on_conflict` is neither `None` nor `"update"`; `only`
                or `exclude` is given on an insert or with `on_conflict`, both
                are given, or one names an unknown field or a relation; or a
                saved instance has no primary key value.

        Note:
            The instance is not reloaded after a partial update, so a field
            left out of `only` keeps its value in memory even if the row
            differs. A copy made with `model_copy()` updates the same row as
            the original. An instance inserted in a transaction that rolled
            back still counts as saved, so its next `save()` raises
            `ModelDoesNotExist`.

        Examples:
            Saving a new instance inserts it:

            ```python {test=run prelude=reference}
            mary = Author(name="Mary Somerville", email="mary@example.com")
            await mary.save()
            assert mary.id == 3
            ```

            Saving a fetched instance with `only=` updates just those columns:

            ```python {test=run prelude=reference}
            post = await Post.get(2)
            post.views = 1000
            post.title = "Not written to the row"
            await post.save(only={"views"})

            rows = await Post.select("title", "views").where(lambda post: post.id == 2).all()
            assert rows[0].title == "The Education of a Computer"
            assert rows[0].views == 1000
            ```
        """
        if on_conflict not in (None, "update"):
            raise ValueError(
                f'on_conflict must be None or "update", got {on_conflict!r}'
            )
        if only is not None:
            normalize_save_columns(only, param="only")
        if exclude is not None:
            normalize_save_columns(exclude, param="exclude")
        if only is not None and exclude is not None:
            raise ValueError(
                "save() accepts only one write-set: only= or exclude=, not both."
            )
        if only is not None or exclude is not None:
            which = "only" if only is not None else "exclude"
            if on_conflict is not None or not _is_persisted(self):
                raise ValueError(
                    f"save({which}=...) is only valid on a persisted UPDATE "
                    "(on_conflict must be None). "
                    "Use Query.update() for set-oriented or expression writes."
                )
        route, identity_using = await _instance_transaction_route(self, using, session)
        new_id = None
        if on_conflict == "update":
            new_id = await save_record(
                self.__class__.__ferro_identity__,
                save_bind_payload(self),
                route,
                mode="upsert",
            )
        elif _is_persisted(self):
            pk_field_name = self.__class__.__ferro_pk__
            pk_val = getattr(self, pk_field_name) if pk_field_name is not None else None
            if pk_val is None:
                raise ValueError(
                    f"Cannot UPDATE a persisted {self.__class__.__name__} "
                    "without a primary key value"
                )
            payload = save_bind_payload(self)
            if only is not None:
                payload = apply_save_only(self, payload, only)
            elif exclude is not None:
                payload = apply_save_exclude(self, payload, exclude)
            rows_affected = await update_record(
                self.__class__.__ferro_identity__,
                payload,
                route,
            )
            if rows_affected == 0:
                raise ModelDoesNotExist(self.__class__, pk_val)
        else:
            new_id = await save_record(
                self.__class__.__ferro_identity__,
                save_bind_payload(self),
                route,
                mode="insert",
            )

        pk_val = None
        pk_field_name = self.__class__.__ferro_pk__

        if pk_field_name is not None:
            spec = self.__class__.__ferro_columns__[pk_field_name]
            if (
                spec.autoincrement
                and getattr(self, pk_field_name) is None
                and new_id is not None
            ):
                setattr(self, pk_field_name, new_id)
            pk_val = getattr(self, pk_field_name)

        if pk_val is not None:
            register_instance(
                self.__class__.__ferro_identity__,
                str(pk_val),
                self,
                route,
            )
            _set_instance_origin(self, identity_using)
        _set_persisted(self, True)

    async def delete(
        self, *, using: str | None = None, session: "Session | None" = None
    ) -> None:
        """Delete the instance's row.

        The instance stays in memory and counts as new again: saving it
        inserts a new row. An instance without a primary key value has no
        row, and the call does nothing.

        Args:
            using: The named connection to delete on. Defaults to `None`, the
                session's connection, or the one the instance was loaded from.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.

        Raises:
            ValueError: `using` names a different connection from the one the
                instance was loaded from.

        Examples:
            ```python {test=run prelude=reference}
            post = await Post.get(2)
            await post.delete()
            assert await Post.get_or_none(2) is None
            ```
        """
        pk_field_name = self.__class__.__ferro_pk__
        pk_val = getattr(self, pk_field_name) if pk_field_name is not None else None
        route, _identity_using = await _instance_transaction_route(self, using, session)

        if pk_val is not None:
            name = self.__class__.__ferro_identity__
            query = Query(self.__class__, using=route.connection_name).where(
                _field_eq(pk_field_name, pk_val)
            )
            await query.delete()
            _core_evict_instance(name, str(pk_val), route)
            # The instance is transient again: a later save() re-INSERTs.
            _set_persisted(self, False)

    @classmethod
    async def all(
        cls, *, using: str | None = None, session: "Session | None" = None
    ) -> list[Self]:
        """Fetch every row of the model's table.

        Args:
            using: The named connection to read from. Defaults to `None`, the
                session's connection.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.

        Returns:
            One instance per row, in the order the database returns them.

        Examples:
            ```python {test=run prelude=reference}
            authors = await Author.all()
            assert sorted(author.name for author in authors) == ["Ada Lovelace", "Grace Hopper"]
            ```
        """
        route = await _transaction_or_using(using, session)
        return await fetch_all(cls, route)

    @classmethod
    async def get(cls, pk: Any, *, session: "Session | None" = None) -> Self:
        """Fetch one record by primary key.

        Args:
            pk: The primary key value to look up.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.

        Returns:
            The record whose primary key is `pk`.

        Raises:
            ModelDoesNotExist: No row has that key. Use
                [`get_or_none`][ferro.Model.get_or_none] to get `None` instead.
            RuntimeError: The model declares no primary key.

        Examples:
            ```python {test=run prelude=reference}
            author = await Author.get(1)
            assert author.name == "Ada Lovelace"
            ```
        """
        instance = await cls.get_or_none(pk, session=session)
        if instance is None:
            raise ModelDoesNotExist(cls, pk)
        return instance

    @classmethod
    async def get_or_none(
        cls, pk: Any, *, session: "Session | None" = None
    ) -> Self | None:
        """Fetch one record by primary key, or `None` when no row has it.

        Args:
            pk: The primary key value to look up.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.

        Returns:
            The record whose primary key is `pk`, or `None`.

        Raises:
            RuntimeError: The model declares no primary key.

        Examples:
            ```python {test=run prelude=reference}
            assert await Author.get_or_none(99) is None
            ```
        """
        pk_field_name = cls.__ferro_pk__
        if pk_field_name is None:
            raise RuntimeError(f"Model {cls.__name__} does not define a primary key")

        return await cls.where(_field_eq(pk_field_name, pk), session=session).first()

    async def refresh(
        self, *, using: str | None = None, session: "Session | None" = None
    ) -> None:
        """Reload the instance's fields from its row, in place.

        A change made in memory and not saved is lost. See
        [`refresh()`](../guide/mutations.md#refresh).

        Args:
            using: The named connection to read from. Defaults to `None`, the
                session's connection, or the one the instance was loaded from.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.

        Raises:
            RuntimeError: The instance has no primary key value, or its row no
                longer exists.

        Examples:
            ```python {test=run prelude=reference}
            post = await Post.get(1)
            post.views = 0
            await post.refresh()
            assert post.views == 1843
            ```
        """
        pk_field_name = self.__class__.__ferro_pk__
        pk_val = getattr(self, pk_field_name) if pk_field_name is not None else None

        if pk_val is None:
            raise RuntimeError("Cannot refresh a model without a primary key")

        name = self.__class__.__ferro_identity__
        route, identity_using = await _instance_transaction_route(self, using, session)

        _core_evict_instance(name, str(pk_val), route)
        query = Query(self.__class__, using=route.connection_name).where(
            _field_eq(pk_field_name, pk_val)
        )
        fresh_instance = await query.first()

        if fresh_instance is None:
            raise RuntimeError(f"Instance not found in database: {name}({pk_val})")

        self.__dict__.update(fresh_instance.__dict__)
        register_instance(name, str(pk_val), self, route)
        _set_instance_origin(self, identity_using)
        _set_persisted(self, True)

    @classmethod
    def where(
        cls, predicate: "Predicate[Self]", *, session: "Session | None" = None
    ) -> Query[Self]:
        """Start a query filtered by a lambda predicate.

        The lambda receives a stand-in for one row; comparing its fields
        builds the SQL condition. Name the parameter after the model, in
        lowercase. A field name the model does not declare raises when the
        query is built. See [Queries](../guide/queries.md#filtering-with-where).

        Args:
            predicate: A lambda that takes the row stand-in and returns a
                condition, such as `lambda post: post.views > 1000`.
            session: The session the query runs in. Defaults to `None`, the
                session opened by the enclosing `async with engines.session():`.

        Returns:
            A query over the matching rows; nothing runs until you await one
            of its methods, such as [`all`][ferro.query.builder.Query.all].

        Examples:
            ```python {test=run prelude=reference}
            popular = await Post.where(lambda post: post.views > 1000).all()
            assert [post.title for post in popular] == ["Notes on the Analytical Engine"]
            ```
        """
        return Query(cls, session=session).where(predicate)

    @overload
    @classmethod
    def select(cls, *, session: "Session | None" = None) -> Query[Self]: ...

    @overload
    @classmethod
    def select(
        cls, selector: "RowSelector[Self]", *, session: "Session | None" = None
    ) -> "ProjectedQuery[Self]": ...

    @overload
    @classmethod
    def select(
        cls, *columns: str, session: "Session | None" = None
    ) -> "ProjectedQuery[Self]": ...

    @classmethod
    def select(
        cls,
        *selectors: "RowSelector[Self] | str",
        session: "Session | None" = None,
    ) -> "Query[Self] | ProjectedQuery[Self]":
        """Start a query, of whole records or of some columns.

        With no argument, the query returns model instances. With a lambda
        that picks fields, or with column names, it returns
        [`Row`][ferro.query.Row] records holding only those columns. See
        [Selecting a column subset](../guide/queries.md#selecting-a-column-subset).

        Args:
            *selectors: Nothing; one lambda returning a field, a tuple of
                fields or a dict naming them; or column names as strings.
            session: The session the query runs in. Defaults to `None`, the
                session opened by the enclosing `async with engines.session():`.

        Returns:
            A [`Query`][ferro.query.builder.Query] with no selector, otherwise a
                [`ProjectedQuery`][ferro.query.builder.ProjectedQuery].

        Raises:
            TypeError: The selectors mix a lambda with column names, or are
                not a lambda or strings.
            ValueError: A selector names a field the model does not have.

        Examples:
            ```python {test=run prelude=reference}
            rows = await Post.select(lambda post: (post.title, post.views)).order_by(lambda post: post.id).all()
            assert rows[0].title == "Notes on the Analytical Engine"
            assert rows[0].views == 1843
            ```
        """
        return Query(cls, session=session).select(*selectors)

    @classmethod
    def using(cls, name: str) -> "ModelConnection[Self]":
        """Run this model's operations on a named connection.

        Args:
            name: The connection's name, as given to `connect(..., name=...)`.

        Returns:
            An object with this model's `create`, `get`, `where` and other
            class methods, each running on `name`.

        Examples:
            ```python {test=run prelude=reference}
            from ferro import connect, engines

            await connect("sqlite::memory:", name="archive", auto_migrate=True)
            async with engines.session("archive"):
                await Author.using("archive").create(name="Mary Somerville", email="mary@example.com")
                archived = await Author.using("archive").all()

            assert [author.name for author in archived] == ["Mary Somerville"]
            ```
        """
        return ModelConnection(cls, name)

    @classmethod
    async def create(cls, *, session: "Session | None" = None, **fields: Any) -> Self:
        """Insert a new record and return it.

        `create` always inserts; use [`upsert`][ferro.Model.upsert] to update
        a row whose primary key exists.

        Args:
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.
            **fields: The field values. A relation field takes the related
                instance or its primary key.

        Returns:
            The saved instance, with a generated primary key filled in.

        Raises:
            UniqueViolationError: A row with the same primary key or unique
                value exists.

        Examples:
            ```python {test=run prelude=reference}
            mary = await Author.create(name="Mary Somerville", email="mary@example.com")
            assert mary.id == 3
            ```
        """
        instance = cls(**fields)
        await instance.save(session=session)
        return instance

    @classmethod
    async def upsert(cls, *, session: "Session | None" = None, **fields: Any) -> Self:
        """Insert a record, or update the row that has its primary key.

        Without a primary key value, an auto-increment key has nothing to
        conflict on and the record is inserted.

        Args:
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.
            **fields: The field values, including the primary key.

        Returns:
            The saved instance.

        Examples:
            ```python {test=run prelude=reference}
            await Author.upsert(id=1, name="Ada King", email="ada@example.com")
            assert (await Author.get(1)).name == "Ada King"
            ```
        """
        instance = cls(**fields)
        await instance.save(session=session, on_conflict="update")
        return instance

    @classmethod
    async def bulk_create(
        cls,
        instances: list[Self],
        *,
        using: str | None = None,
        session: "Session | None" = None,
    ) -> int:
        """Insert many new records in one call.

        Every record is inserted or none is. Ferro splits a batch too large
        for one statement into several, inside the same all-or-nothing
        boundary. See
        [Creating rows](../guide/mutations.md#creating-rows).

        Args:
            instances: The instances to insert.
            using: The named connection to write on. Defaults to `None`, the
                session's connection.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.

        Returns:
            The number of rows inserted.

        Examples:
            ```python {test=run prelude=reference}
            inserted = await Author.bulk_create(
                [
                    Author(name="Mary Somerville", email="mary@example.com"),
                    Author(name="Hedy Lamarr", email="hedy@example.com"),
                ]
            )
            assert inserted == 2
            ```
        """
        if not instances:
            return 0
        data = [save_bind_payload(i) for i in instances]
        route = await _transaction_or_using(using, session)
        return await save_bulk_records(cls.__ferro_identity__, data, route)

    @classmethod
    async def get_or_create(
        cls,
        defaults: dict[str, Any] | None = None,
        *,
        session: "Session | None" = None,
        **fields: Any,
    ) -> tuple[Self, bool]:
        """Fetch the first record whose fields match, or create it.

        Args:
            defaults: Extra field values used only when creating. Defaults to
                `None`.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.
            **fields: The values to match exactly, also used when creating.

        Returns:
            The record, and `True` when it was created or `False` when it
                was found.

        Examples:
            ```python {test=run prelude=reference}
            author, created = await Author.get_or_create(
                email="mary@example.com", defaults={"name": "Mary Somerville"}
            )
            assert created is True

            again, created = await Author.get_or_create(email="mary@example.com")
            assert created is False
            assert again is author
            ```
        """
        query = Query(cls, session=session)
        for key, val in fields.items():
            query = query.where(_field_eq(key, val))

        instance = await query.first()
        if instance:
            return instance, False

        params = {**fields, **(defaults or {})}
        return await cls.create(session=session, **params), True

    @classmethod
    async def update_or_create(
        cls,
        defaults: dict[str, Any] | None = None,
        *,
        session: "Session | None" = None,
        **fields: Any,
    ) -> tuple[Self, bool]:
        """Update the first record whose fields match, or create it.

        Args:
            defaults: The field values to write, on the found record or the
                new one. Defaults to `None`.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.
            **fields: The values to match exactly, also used when creating.

        Returns:
            The record, and `True` when it was created or `False` when it
                was updated.

        Examples:
            ```python {test=run prelude=reference}
            author, created = await Author.update_or_create(
                email="ada@example.com", defaults={"name": "Ada King"}
            )
            assert created is False
            assert author.name == "Ada King"
            ```
        """
        query = Query(cls, session=session)
        for key, val in fields.items():
            query = query.where(_field_eq(key, val))

        instance = await query.first()
        if instance:
            for key, val in (defaults or {}).items():
                setattr(instance, key, val)
            await instance.save(session=session)
            return instance, False

        params = {**fields, **(defaults or {})}
        return await cls.create(session=session, **params), True


class ModelConnection[M: Model]:
    """A model's class methods bound to one named connection.

    [`Model.using`][ferro.Model.using] returns one. Each method behaves like
    the model's class method of the same name and runs on the bound
    connection.
    """

    def __init__(self, model_cls: type[M], connection_name: str) -> None:
        """Bind a model class to a connection.

        Args:
            model_cls: The model class.
            connection_name: The name of the connection to run on.
        """
        self.model_cls: type[M] = model_cls
        """The bound model class."""
        self._connection_name: str = connection_name

    async def create(self, **fields: Any) -> M:
        """Insert a new record on the connection and return it.

        Args:
            **fields: The field values.

        Returns:
            The saved instance.

        Raises:
            UniqueViolationError: A row with the same primary key or unique
                value exists.
        """
        instance = self.model_cls(**fields)
        await instance.save(using=self._connection_name)
        return instance

    async def upsert(self, **fields: Any) -> M:
        """Insert a record on the connection, or update the row with its primary key.

        Args:
            **fields: The field values, including the primary key.

        Returns:
            The saved instance.
        """
        instance = self.model_cls(**fields)
        await instance.save(using=self._connection_name, on_conflict="update")
        return instance

    async def all(self) -> list[M]:
        """Fetch every row of the model's table on the connection.

        Returns:
            One instance per row.
        """
        return await self.model_cls.all(using=self._connection_name)

    def select(self) -> Query[M]:
        """Start a query of whole records on the connection.

        Returns:
            A query over every row.
        """
        return Query(self.model_cls, using=self._connection_name)

    def where(self, predicate: "Predicate[M]") -> Query[M]:
        """Start a query on the connection, filtered by a lambda predicate.

        Args:
            predicate: A lambda that takes the row stand-in and returns a
                condition.

        Returns:
            A query over the matching rows.
        """
        return self.select().where(predicate)

    async def get(self, pk: Any) -> M:
        """Fetch one record by primary key on the connection.

        Args:
            pk: The primary key value to look up.

        Returns:
            The record whose primary key is `pk`.

        Raises:
            ModelDoesNotExist: No row has that key.
        """
        instance = await self.get_or_none(pk)
        if instance is None:
            raise ModelDoesNotExist(self.model_cls, pk)
        return instance

    async def get_or_none(self, pk: Any) -> M | None:
        """Fetch one record by primary key on the connection, or `None`.

        Args:
            pk: The primary key value to look up.

        Returns:
            The record whose primary key is `pk`, or `None`.

        Raises:
            RuntimeError: The model declares no primary key.
        """
        pk_field_name = self.model_cls.__ferro_pk__
        if pk_field_name is None:
            raise RuntimeError(
                f"Model {self.model_cls.__name__} does not define a primary key"
            )

        return await self.where(_field_eq(pk_field_name, pk)).first()

    async def bulk_create(self, instances: list[M]) -> int:
        """Insert many new records on the connection in one call.

        Args:
            instances: The instances to insert.

        Returns:
            The number of rows inserted.
        """
        return await self.model_cls.bulk_create(instances, using=self._connection_name)

    async def get_or_create(
        self, defaults: dict[str, Any] | None = None, **fields: Any
    ) -> tuple[M, bool]:
        """Fetch the first matching record on the connection, or create it.

        Args:
            defaults: Extra field values used only when creating. Defaults to
                `None`.
            **fields: The values to match exactly, also used when creating.

        Returns:
            The record, and whether it was created.
        """
        query = Query(self.model_cls, using=self._connection_name)
        for key, val in fields.items():
            query = query.where(_field_eq(key, val))

        instance = await query.first()
        if instance:
            return instance, False

        params = {**fields, **(defaults or {})}
        return await self.create(**params), True

    async def update_or_create(
        self, defaults: dict[str, Any] | None = None, **fields: Any
    ) -> tuple[M, bool]:
        """Update the first matching record on the connection, or create it.

        Args:
            defaults: The field values to write, on the found record or the
                new one. Defaults to `None`.
            **fields: The values to match exactly, also used when creating.

        Returns:
            The record, and whether it was created.
        """
        query = Query(self.model_cls, using=self._connection_name)
        for key, val in fields.items():
            query = query.where(_field_eq(key, val))

        instance = await query.first()
        if instance:
            for key, val in (defaults or {}).items():
                setattr(instance, key, val)
            await instance.save(using=self._connection_name)
            return instance, False

        params = {**fields, **(defaults or {})}
        return await self.create(**params), True
