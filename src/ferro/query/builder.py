"""Query builders: [`Query`][ferro.query.builder.Query], its projected form and relation collections.

Each chained call validates its arguments and returns a new query; nothing
reaches the database until a query is awaited through one of its methods.
"""

# This module owns chain-time state and validation (predicates, joins,
# projection, includes). Everything whose output is wire shape lives behind
# ``wire.compile_query``.

import copy
from collections.abc import Awaitable
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Generic,
    Literal,
    NamedTuple,
    Never,
    NoReturn,
    Protocol,
    Self,
    Type,
    TypeVar,
    cast,
    overload,
)

from .._core import (
    add_m2m_links,
    clear_m2m_links,
    count_filtered,
    delete_filtered,
    fetch_filtered,
    remove_m2m_links,
    render_count,
    render_delete,
    render_fetch,
    render_update,
    update_filtered,
)
from .nodes import (
    AggregateExpr,
    FieldProxy,
    Predicate,
    QueryNode,
    QueryProxy,
    RelationProxy,
    ReverseRelationProxy,
    RowSelector,
    ValueExpr,
    validate_query_column,
)
from .rendered import Terminal, send, terminal
from .rows import Row, Rows
from .wire import (
    M2mContext,
    OrderByEntry,
    compile_query,
    model_identity,
    path_edges,
)

if TYPE_CHECKING:
    from .._core import RouteHandle

T = TypeVar("T")
"""The model class a query returns."""
E = TypeVar("E")
"""The element type of a relation collection."""

# The concrete container type a projected query delivers (parameterized once
# at import; `into=` record types would parameterize per call). Construction
# always goes through Rows._wrap — never pydantic validation (ADR-0007).
_ROWS_OF_ROW: type[Rows[Row]] = Rows[Row]


def _dotted_order_key(entry: OrderByEntry) -> str:
    """``account.label`` for a traversed key, ``id`` for a root key."""
    return ".".join((*entry.path, entry.column))


def _read_instance_order_value(row: Any, entry: OrderByEntry) -> Any:
    """Walk a populated relation path; unpopulated is a loud build-time error.

    Include-population puts the relation name in the instance ``__dict__``,
    shadowing the class-level ``ForwardDescriptor``. A missing key is the
    awaitable (unpopulated) contract — do not follow the descriptor.
    """
    current = row
    for hop in entry.path:
        if hop not in vars(current):
            dotted = _dotted_order_key(entry)
            raise ValueError(
                f"position_of() requires relation path {dotted!r} populated "
                "on the instance; pass a tuple if you only have the values"
            )
        hop_value = vars(current)[hop]
        if hop_value is None:
            return None
        current = hop_value
    return getattr(current, entry.column)


def _normalize_order_by_nulls(nulls: str | None) -> str:
    """Lower-case and validate ``nulls=``; omitted means ``last`` on the wire (#392)."""
    if nulls is None:
        return "last"
    normalized = nulls.lower()
    if normalized not in ("first", "last", "native"):
        raise ValueError("nulls must be 'first', 'last', or 'native'")
    return normalized


def _resolve_where_node(predicate: "Predicate[Any]", model_cls: type) -> QueryNode:
    """Evaluate a lambda predicate against a validating ``QueryProxy``."""
    if not callable(predicate):
        raise TypeError(
            "where() expected a predicate callable "
            f"(e.g. `lambda user: user.age >= 18`), got {type(predicate).__name__}"
        )
    result = predicate(QueryProxy(model_cls))
    if isinstance(result, RelationProxy):
        relation = result._path[-1]
        raise TypeError(
            f"where() predicate returned the bare relation {relation!r}; compare "
            f"a column (e.g. t.{relation}.<column> == ...) or use == None / "
            "== an instance to filter by the relation."
        )
    if isinstance(result, ReverseRelationProxy):
        relation = result._name
        raise TypeError(
            f"where() predicate returned the bare reverse relation "
            f"{relation!r}; test membership with t.{relation}.exists() "
            f"(negate with ~t.{relation}.exists(), ADR-0007)."
        )
    if isinstance(result, AggregateExpr):
        # Comparing an aggregate already raises inside the lambda (#294); a
        # BARE aggregate returned from where() gets the same having() story.
        raise TypeError(
            f"where() cannot filter on the aggregate {result._dotted()}: "
            "WHERE filters rows before aggregation. Post-aggregation "
            "filtering is having() (#291)."
        )
    if isinstance(result, ValueExpr):
        result._reject_clause("where")
    if not isinstance(result, QueryNode):
        raise TypeError(
            "where() predicate callable must return QueryNode, "
            f"got {type(result).__name__}"
        )
    return result


def _resolve_update_recipe(
    recipe: Callable[[QueryProxy[Any]], Any], model_cls: type
) -> dict[str, Any]:
    """Evaluate an ``update()`` recipe against a validating ``QueryProxy``."""
    if not callable(recipe):
        raise TypeError(
            "update() expected a recipe callable "
            '(e.g. `lambda user: {"email": "x", "bonus": user.score}`), '
            f"got {type(recipe).__name__}"
        )
    result = recipe(QueryProxy(model_cls))
    if not isinstance(result, dict):
        raise TypeError(
            f"update() recipe must return a dict, got {type(result).__name__}"
        )
    if not result:
        raise ValueError("update() recipe must assign at least one column")
    return result


def _register_join_paths(node: QueryNode, joins: dict[tuple[str, ...], str]) -> None:
    """Register every relation path a resolved WHERE node references (#270).

    Walks the node tree depth-first, left-to-right; each leaf with a non-empty
    ``path`` registers that FULL path once (``setdefault`` keeps the first
    occurrence's order). Only full paths are stored — the Rust walker dedups
    shared prefixes when it renders JOINs, so a ``["account", "owner"]`` leaf
    and an ``["account"]`` leaf still yield exactly two JOINs. Dedup across
    ``&``/``|`` trees and across multiple ``where()`` calls falls out of the
    dict.
    """
    if node.child is not None:
        _register_join_paths(node.child, joins)
        return
    if node.is_compound:
        if node.left is not None:
            _register_join_paths(node.left, joins)
        if node.right is not None:
            _register_join_paths(node.right, joins)
        return
    if node.path:
        joins.setdefault(tuple(node.path), "inner")


def _resolve_join_selector(
    selector: "Callable[[QueryProxy[Any]], Any]", model_cls: type
) -> tuple[str, ...]:
    """Resolve a join-chainer selector into a relation path (#272).

    ``selector`` is a lambda receiving a validating `QueryProxy` and
    naming a RELATION path (``lambda t: t.account``, ``lambda t: t.account.owner``)
    — i.e. it must return a `RelationProxy`. Each hop is validated against
    the relevant model at build time (the proxy raises ``AttributeError`` with a
    did-you-mean for a bad hop, same as ``where()``).

    Returns:
        The relation ``path`` tuple the selector names (length ≥ 1).

    Raises:
        TypeError: If ``selector`` is not callable, resolves to a column
            (`FieldProxy`) rather than a relation, or returns any other
            non-relation value — a join selector names a relation path, not a
            column.
    """
    if not callable(selector):
        raise TypeError(
            "join()/left_join() expected a selector callable "
            f"(e.g. `lambda t: t.account`), got {type(selector).__name__}"
        )
    result = selector(QueryProxy(model_cls))
    if isinstance(result, FieldProxy):
        raise TypeError(
            "join()/left_join() selector resolved to a column, not a relation "
            "(e.g. `lambda t: t.account.name`); a join selector names a relation "
            "path (e.g. `lambda t: t.account`)."
        )
    if isinstance(result, ReverseRelationProxy):
        # Joining a reverse/M2M edge would multiply root rows — the pinned
        # "a join never multiplies root rows" property is preserved by
        # rejection (ADR-0007); membership is the existence test's job.
        relation = result._name
        raise TypeError(
            f"join()/left_join() cannot join the reverse relation "
            f"{relation!r}: a join on a reverse or many-to-many edge would "
            "multiply root rows. Test membership with a predicate instead — "
            f"where(lambda t: t.{relation}.exists(...)), negated with ~ — "
            "reverse relations are tested, not traversed (ADR-0007)."
        )
    if not isinstance(result, RelationProxy):
        raise TypeError(
            "join()/left_join() selector must return a relation path "
            f"(e.g. `lambda t: t.account`), got {type(result).__name__}"
        )
    return result._path


def _reject_reverse_relation_include(exc: AttributeError) -> None:
    """Raise pointedly when an include selector names a BackRef/M2M (#287).

    The validating proxies raise ``AttributeError`` for any name that is not
    a column or a forward relation; when the failing name (carried
    structurally on the exception) is a declared reverse or many-to-many
    relation of the hop's model, include() has a sharper story than
    "no queryable column": reverse population is a separate future mechanism
    — a batched second query stitched onto the results — deliberately not
    ``include()``.
    """
    from ..relations.descriptors import RelationshipDescriptor

    name = getattr(exc, "name", None)
    model = getattr(exc, "obj", None)
    if not name or model is None:
        return
    if isinstance(getattr(model, name, None), RelationshipDescriptor):
        raise _reverse_population_error(name) from None


def _reverse_population_error(name: str) -> TypeError:
    """The pinned include()-cannot-populate-a-reverse-relation error (#287).

    One construction site for the two paths that reach it: a reverse relation
    the predicate proxies resolve (a `ReverseRelationProxy` selector
    result, #314) and one they don't yet (an M2M name, surfaced as an
    ``AttributeError`` and re-raised by
    `_reject_reverse_relation_include`).
    """
    return TypeError(
        f"include() cannot populate {name!r}: it is a reverse (BackRef) "
        "or many-to-many relation, and include() populates forward "
        "foreign-key paths only (e.g. lambda t: t.account). Reverse and "
        "M2M population will be a separate mechanism — a batched second "
        "query stitched onto the results — not include(). Until it "
        f"lands, fetch the collection through the relation itself "
        f"(await instance.{name}.all())."
    )


def _resolve_include_selector(
    selector: "Callable[[QueryProxy[Any]], Any]", model_cls: type
) -> tuple[str, ...]:
    """Resolve an ``include()`` selector into a relation path (#286).

    ``selector`` is a lambda receiving a validating `QueryProxy` and
    naming a forward-FK RELATION path (``lambda t: t.account``,
    ``lambda t: t.account.owner``) — the same traversal syntax as ``join()``,
    resolving to a `RelationProxy`. Each hop is validated against the
    relevant model at build time (the proxy raises ``AttributeError`` with a
    did-you-mean for a bad hop, exactly like ``where()``).

    Returns:
        The relation ``path`` tuple the selector names (length ≥ 1).

    Raises:
        TypeError: If ``selector`` is a string or otherwise not callable
            (strings never traverse, #280 — include is lambda-selector only),
            resolves to a column (`FieldProxy`) rather than a relation,
            names a BackRef/M2M relation (reverse population is a separate
            future mechanism, #287), or returns any other non-relation value.
    """
    if isinstance(selector, str):
        raise TypeError(
            f"include() takes a lambda selector naming a relation path, not a "
            f"string: include({selector!r}) is not supported (strings never "
            f"traverse). Use include(lambda t: t.{selector})."
        )
    if not callable(selector):
        raise TypeError(
            "include() expected a selector callable "
            f"(e.g. `lambda t: t.account`), got {type(selector).__name__}"
        )
    try:
        result = selector(QueryProxy(model_cls))
    except AttributeError as exc:
        _reject_reverse_relation_include(exc)
        raise
    if isinstance(result, FieldProxy):
        dotted = ".".join((*result.path, result.column))
        raise TypeError(
            f"include() selector resolved to the column {dotted!r}, not a "
            "relation; include() populates whole related instances "
            "(e.g. `lambda t: t.account`) — every populated hop is a complete "
            "row, so there is nothing to select per column."
        )
    if isinstance(result, ReverseRelationProxy):
        # The predicate proxies resolve reverse relations now (#314), so the
        # population rejection fires here rather than via AttributeError.
        raise _reverse_population_error(result._name)
    if not isinstance(result, RelationProxy):
        raise TypeError(
            "include() selector must return a relation path "
            f"(e.g. `lambda t: t.account`), got {type(result).__name__}"
        )
    return result._path


class _ProjectedField(NamedTuple):
    """One projected record field, resolved at build time (#279/#293/#294).

    ``name`` is the output field name on the record — the dict key when the
    selector is dict-returning (an output alias, GLOSSARY.md), the bare leaf
    column name otherwise. ``column`` is the source column and ``path`` the
    relation path to its table (``()`` = root model; non-empty is traversed
    projection). ``agg`` is the aggregate function applied to the source
    (``None`` = a plain column field); an aggregate field serializes as a v5
    ``expr`` — its traversal rides ``expr.path`` as hop facts, never the
    field-level ``path``.
    """

    name: str
    column: str
    path: tuple[str, ...]
    agg: str | None = None

    @property
    def dotted(self) -> str:
        """The selector expression this field came from (error messages)."""
        base = "t." + ".".join((*self.path, self.column))
        return f"{base}.{self.agg}()" if self.agg else base


def _resolve_projection_selector(
    selector: "RowSelector[Any]", model_cls: type
) -> tuple[_ProjectedField, ...]:
    """Resolve a ``select()`` lambda selector into projected fields (#279/#293).

    ``selector`` receives a validating `QueryProxy` and returns one
    field (``lambda t: t.amount``), a tuple/list of fields
    (``lambda t: (t.id, t.account.name)``), or a dict whose keys name the
    output fields (``lambda t: {"account_name": t.account.name}`` — output
    aliases, GLOSSARY.md). Fields may traverse declared forward-FK relations
    at any depth; every hop validates at build time with did-you-mean,
    exactly like ``where()``/``order_by()``. Mixed nesting — a dict inside a
    tuple, a tuple inside a dict — is rejected: one selector, one shape.

    Returns:
        The projected fields, in selection/insertion order.

    Raises:
        TypeError: If an element is a bare relation (``lambda t: t.account``),
            a comparison (``lambda t: t.id == 1``), a nested dict/tuple, a
            non-string dict key, or any other non-field value.
        ValueError: If the selection is empty, or two fields resolve to the
            same output name (the collision error names the dict form).
    """
    result = selector(QueryProxy(model_cls))
    if isinstance(result, dict):
        fields = _collect_dict_projection(result)
    else:
        items = tuple(result) if isinstance(result, (tuple, list)) else (result,)
        fields = _collect_tuple_projection(items)
    if not fields:
        raise ValueError(
            "select() projection selected no columns; select at least one "
            "(e.g. `lambda t: (t.id, t.amount)`), or call select() with no "
            "arguments for the full query."
        )
    seen: dict[str, _ProjectedField] = {}
    for field in fields:
        first = seen.get(field.name)
        if first is not None:
            raise ValueError(
                f"select() projects two fields named {field.name!r} "
                f"({first.dotted} and {field.dotted}); output names must be "
                "unique. Name them explicitly with the dict selector form "
                f'(e.g. select(lambda t: {{"{first.name}": {first.dotted}, '
                f'"<other name>": {field.dotted}}})).'
            )
        seen[field.name] = field
    return fields


def _resolve_projection_strings(
    names: tuple[str, ...], model_cls: type
) -> tuple[str, ...]:
    """Resolve ``select("id", "amount")`` string selectors (#280).

    Exactly ``order_by``'s string contract: root columns only (declared fields
    plus shadow ``{fk}_id`` columns), validated at build time with
    did-you-mean. Strings never traverse — a dotted path is rejected
    pointedly, permanently (PRD #277); traversed projection is lambda-only
    (#293).

    Raises:
        ValueError: For a dotted path, or a column named twice.
        AttributeError: For a name that is not a queryable column
            (did-you-mean, from `validate_query_column`).
    """
    columns: list[str] = []
    for name in names:
        if "." in name:
            raise ValueError(
                f"select() string selectors name root columns only and never "
                f"traverse: {name!r} is not a column. String paths are "
                "rejected permanently; traversed projection is lambda-only "
                f"(select(lambda t: t.{name})). Select root columns "
                '(e.g. select("id", "amount")).'
            )
        validate_query_column(model_cls, name)
        if name in columns:
            raise ValueError(
                f"select() projects column {name!r} more than once; "
                "each projected column must be unique."
            )
        columns.append(name)
    return tuple(columns)


def _projection_item_field(item: Any, *, context: str) -> _ProjectedField:
    """Validate one selector item into an unaliased field (#279/#293).

    Shared by the single-field and tuple forms; ``context`` names the
    selector shape in error messages. The output name defaults to the bare
    leaf column name (GLOSSARY.md: Traversed projection).
    """
    if isinstance(item, dict):
        raise TypeError(
            "select() selector cannot nest a dict inside a tuple; a selector "
            "returns one shape — a dict, a tuple, or a single field. To name "
            "output fields, return one dict for the whole selection "
            '(e.g. `lambda t: {"id": t.id, "account_name": t.account.name}`).'
        )
    if isinstance(item, AggregateExpr):
        raise TypeError(
            f"aggregate fields are user-named (#294): give {item._dotted()} "
            "an output name with the dict selector form "
            f'(e.g. select(lambda t: {{"total": {item._dotted()}}})).'
        )
    if isinstance(item, ValueExpr):
        item._reject_clause("select")
    if isinstance(item, RelationProxy):
        relation = item._path[-1]
        raise TypeError(
            f"select() cannot project the bare relation {relation!r}; "
            f"select a column on it instead (e.g. t.{relation}.<column>) "
            "or select root columns."
        )
    if isinstance(item, QueryNode):
        raise TypeError(
            "select() selector returned a comparison, not a column "
            "(e.g. `lambda t: t.id == 1`); projections select columns "
            "(`lambda t: t.id`) — put predicates in where()."
        )
    if not isinstance(item, FieldProxy):
        raise TypeError(
            f"select() selector must return {context} "
            f"(e.g. `lambda t: (t.id, t.amount)`), got {type(item).__name__}"
        )
    return _ProjectedField(name=item.column, column=item.column, path=item.path)


def _collect_tuple_projection(items: tuple[Any, ...]) -> tuple[_ProjectedField, ...]:
    """Validate a single-field or tuple selector's items into fields (#293)."""
    return tuple(
        _projection_item_field(item, context="a column or a tuple of columns")
        for item in items
    )


def _collect_dict_projection(result: dict[Any, Any]) -> tuple[_ProjectedField, ...]:
    """Validate a dict selector into aliased fields (#293).

    Keys are output field names (GLOSSARY.md: Output alias) and must be
    strings; values are field references (which may traverse) or aggregate
    expressions (``t.amount.sum()`` — #294, dict-only: aggregate fields are
    user-named). Nesting a tuple, list, or dict as a value is rejected —
    flat records only (ADR-0009: record results are flat).
    """
    fields: list[_ProjectedField] = []
    for key, value in result.items():
        if not isinstance(key, str):
            raise TypeError(
                f"select() dict selector keys are output field names and must "
                f"be strings, got {key!r} ({type(key).__name__})."
            )
        if isinstance(value, AggregateExpr):
            fields.append(
                _ProjectedField(
                    name=key, column=value.column, path=value.path, agg=value.fn
                )
            )
            continue
        if isinstance(value, ValueExpr):
            value._reject_clause("select")
        if isinstance(value, (dict, tuple, list)):
            raise TypeError(
                f"select() dict selector value for {key!r} is a "
                f"{type(value).__name__}; a projected record is flat — each "
                "dict value names one field (e.g. "
                '`lambda t: {"account_name": t.account.name}`).'
            )
        if isinstance(value, RelationProxy):
            relation = value._path[-1]
            raise TypeError(
                f"select() cannot project the bare relation {relation!r} "
                f"(dict key {key!r}); select a column on it instead "
                f"(e.g. {{{key!r}: t.{relation}.<column>}})."
            )
        if isinstance(value, QueryNode):
            raise TypeError(
                f"select() dict selector value for {key!r} is a comparison, "
                "not a column; projections select columns — put predicates "
                "in where()."
            )
        if not isinstance(value, FieldProxy):
            raise TypeError(
                f"select() dict selector value for {key!r} must be a column "
                f"reference, got {type(value).__name__}."
            )
        fields.append(_ProjectedField(name=key, column=value.column, path=value.path))
    return tuple(fields)


if TYPE_CHECKING:
    from .rendered import RenderedSql

    class _UpdateTerminal(Protocol[E]):
        """`query.update` as a type checker reads it: a recipe or keyword
        literals, never both, on the call and on `.sql()` alike."""

        @overload
        def __call__(
            self, recipe: Callable[[QueryProxy[E]], dict[str, Any]], /
        ) -> Awaitable[int]: ...
        @overload
        def __call__(self, /, **fields: Any) -> Awaitable[int]: ...
        @overload
        def sql(
            self, recipe: Callable[[QueryProxy[E]], dict[str, Any]], /
        ) -> Awaitable[RenderedSql]: ...
        @overload
        def sql(self, /, **fields: Any) -> Awaitable[RenderedSql]: ...

    class _UpdateMethod(Protocol):
        """`Query.update` as a type checker reads it: reading it from a
        `Query[E]` binds an `_UpdateTerminal[E]`."""

        @overload
        def __get__(self, query: None, owner: Any) -> "_UpdateMethod": ...
        @overload
        def __get__(self, query: "Query[E]", owner: Any) -> _UpdateTerminal[E]: ...


def _update_terminal(method: Callable[..., Awaitable[int]]) -> "_UpdateMethod":
    """Make `Query.update` a [`terminal`][ferro.query.rendered.terminal] that
    keeps its two forms apart statically.

    A terminal carries its method's one signature, and `update` has two
    (a recipe, or keyword literals), so a type checker reads this terminal
    through `_UpdateMethod`, whose call and `.sql()` are overloaded per form.
    At runtime it is `terminal(method)`.
    """
    return cast("_UpdateMethod", terminal(method))


class Query(Generic[T]):
    """A lazy query over one model's rows.

    Start one from the model with [`Model.where`][ferro.Model.where] or
    [`Model.select`][ferro.Model.select]. Each chained method returns a new
    query and leaves the original unchanged; nothing runs until you await
    [`all`][ferro.query.builder.Query.all], [`first`][ferro.query.builder.Query.first],
    [`count`][ferro.query.builder.Query.count], [`exists`][ferro.query.builder.Query.exists],
    [`update`][ferro.query.builder.Query.update] or
    [`delete`][ferro.query.builder.Query.delete]. See [Queries](../guide/queries.md).

    Examples:
        ```python {test=run prelude=reference}
        query = Post.where(lambda post: post.views > 900)
        newest_first = query.order_by(lambda post: post.id, "desc")

        posts = await newest_first.all()
        assert [post.id for post in posts] == [2, 1]
        assert await query.count() == 2
        ```
    """

    def __init__(
        self, model_cls: Type[T], using: str | None = None, session: Any | None = None
    ):
        """Start a query over every row of a model.

        Args:
            model_cls: The model class whose table the query reads.
            using: The named connection to run on. Defaults to `None`, the
                session's connection.
            session: The session to run in. Defaults to `None`, the session
                opened by the enclosing `async with engines.session():`.
        """
        self.model_cls = model_cls
        """The model class whose rows the query returns."""
        self._using = using
        self._session = session
        self.where_clause: list["QueryNode"] = []
        """The conditions added by `where()`, combined with `AND`."""
        self.order_by_clause: list[OrderByEntry] = []
        """The sort keys added by `order_by()`, in the order added."""
        self._limit: int | None = None
        self._offset: int | None = None
        self._after: tuple[Any, ...] | None = None
        self._before: tuple[Any, ...] | None = None
        self._m2m_context: M2mContext | None = None
        # Relation paths that must render a join, insertion-ordered (full path
        # tuple -> registered join_type). Populated by where()/order_by()
        # traversal ("inner") and by the explicit join()/left_join() chainers
        # ("inner"/"left"). Serialized into the QueryIR ``joins`` section by
        # all()/count() (#270, #272).
        self._joins: dict[tuple[str, ...], str] = {}
        # Edges (path prefixes, length ≥ 1) whose join type was fixed by an
        # explicit chainer, insertion-ordered (edge tuple -> "inner"|"left").
        # ``.left_join`` marks every edge of its path "left" (whole-path rule,
        # ADR-0006); ``.join`` marks them "inner". The single source of truth
        # for LEFT on the wire — implicit where()/order_by() traversal never
        # touches this, so explicit always beats implicit (#272).
        self._explicit_edges: dict[tuple[str, ...], str] = {}
        # Relation paths to populate (#286, ADR-0008), insertion-ordered and
        # deduped by path identity (dict-as-ordered-set). CRITICAL: include
        # paths never enter ``_joins``/the wire ``joins`` section — they ride the
        # ``instances`` materialization plan, so the ``joins`` wire section
        # keeps its stage-1 semantics untouched and include contributes no
        # join-type opinion on any edge another clause references.
        self._includes: dict[tuple[str, ...], None] = {}

    async def _transaction_or_using(self) -> "RouteHandle":
        from .. import _ensure_rust_registration_synced_for_operation
        from ..state import resolve_operation_scope

        await _ensure_rust_registration_synced_for_operation()
        return resolve_operation_scope(using=self._using, session=self._session)

    def _clone(self) -> Self:
        """Return a copy of this query with no shared mutable state.

        ``copy.copy`` preserves the concrete class (``Relation`` stays
        ``Relation``); the mutable containers are then replaced so chained
        queries never alias the originals (FF-F F-1).
        """
        new = copy.copy(self)
        new.where_clause = list(self.where_clause)
        new.order_by_clause = list(self.order_by_clause)
        # M2mContext is frozen, so clones can share it safely.
        new._m2m_context = self._m2m_context
        new._joins = dict(self._joins)
        new._explicit_edges = dict(self._explicit_edges)
        new._includes = dict(self._includes)
        return new

    def _m2m(
        self, join_table: str, source_col: str, target_col: str, source_id: Any
    ) -> Self:
        """Store many-to-many linkage context for relationship operations.

        A new ``Query`` with the m2m context set; ``self`` is unchanged.
        """
        new = self._clone()
        new._m2m_context = M2mContext(
            join_table=join_table,
            source_col=source_col,
            target_col=target_col,
            source_id=source_id,
        )
        return new

    def where(self, predicate: "Predicate[T]") -> Self:
        """Add a condition that every returned row must meet.

        The lambda receives a stand-in for one row, a
        [`QueryProxy`][ferro.query.QueryProxy]; comparing its fields builds
        the SQL condition. Conditions from several `where()` calls are
        combined with `AND`. A field name the model does not declare raises
        `AttributeError` with the closest match, before any SQL runs.

        A field that is a foreign key relation reaches into the related
        model (`lambda post: post.author.name == "Ada Lovelace"`), and each
        relation path adds one `INNER JOIN`. See
        [Querying across relationships](../guide/queries.md#querying-across-relationships).

        Args:
            predicate: A lambda that takes the row stand-in and returns a
                condition, such as `lambda post: post.views > 1000`.

        Returns:
            A new query with the condition added.

        Raises:
            TypeError: `predicate` is not callable or does not return a
                condition.
            AttributeError: The lambda names a field the model does not have.

        Examples:
            ```python {test=run prelude=reference}
            posts = await Post.where(lambda post: post.author.name == "Grace Hopper").all()
            assert [post.title for post in posts] == ["The Education of a Computer"]
            ```
        """
        new = self._clone()
        node = _resolve_where_node(predicate, self.model_cls)
        new.where_clause.append(node)
        _register_join_paths(node, new._joins)
        return new

    @overload
    def select(self) -> Self: ...

    @overload
    def select(self, selector: "RowSelector[T]") -> "ProjectedQuery[T]": ...

    @overload
    def select(self, *columns: str) -> "ProjectedQuery[T]": ...

    def select(self, *selectors: "RowSelector[T] | str") -> "Self | ProjectedQuery[T]":
        """Return only some columns, as records, or the query unchanged.

        With no argument, `select()` returns the same query of whole model
        instances. With a lambda or column names, the query returns
        [`Row`][ferro.query.Row] records in a [`Rows`][ferro.query.Rows] list,
        never model instances. The lambda returns one field, a tuple of
        fields, or a dict whose keys name the output fields; a field may
        reach into a related model (`post.author.name`). Column names as
        strings name the model's own columns only. See
        [Selecting a column subset](../guide/queries.md#selecting-a-column-subset).

        Args:
            *selectors: Nothing; one lambda returning a field, a tuple of
                fields or a dict naming them; or column names as strings.

        Returns:
            The query unchanged with no selector, otherwise a
                [`ProjectedQuery`][ferro.query.builder.ProjectedQuery].

        Raises:
            TypeError: The selectors mix a lambda with column names, select a
                relation or a comparison instead of a field, or nest a dict
                and a tuple.
            ValueError: The selection is empty, two fields share an output
                name, a string names a related column, or the query already
                has an `include()`.

        Examples:
            A tuple keeps each field's own name:

            ```python {test=run prelude=reference}
            rows = await Post.select(lambda post: (post.title, post.views)).order_by(lambda post: post.id).all()
            assert rows[0].title == "Notes on the Analytical Engine"
            assert rows[0].views == 1843
            ```

            A dict names the output fields, and may reach into a related model:

            ```python {test=run prelude=reference}
            rows = await Post.select(
                lambda post: {"post": post.title, "author": post.author.name}
            ).order_by(lambda post: post.id).all()
            assert rows[1].author == "Grace Hopper"
            ```
        """
        if not selectors:
            return self._clone()
        if self._includes:
            raise ValueError(
                "select() with a projection cannot be combined with "
                "include(): a query carries exactly one materialization plan "
                "— projected records or populated instances, never both, and "
                "record results are flat (ADR-0009). Reach across the "
                "relation with traversed projection instead (e.g. "
                'select(lambda t: {"account_name": t.account.name})), or '
                "populate instances with include() on an unprojected query."
            )
        if any(isinstance(selector, str) for selector in selectors):
            if not all(isinstance(selector, str) for selector in selectors):
                raise TypeError(
                    "select() cannot mix column-name strings and a lambda "
                    "selector in one call; use one form "
                    '(select("id", "amount") or '
                    "select(lambda t: (t.id, t.amount)))."
                )
            names: tuple[str, ...] = selectors  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
            columns = _resolve_projection_strings(names, self.model_cls)
            fields = tuple(
                _ProjectedField(name=column, column=column, path=())
                for column in columns
            )
            return ProjectedQuery(self, fields)
        if len(selectors) > 1:
            raise TypeError(
                "select() takes a single lambda selector naming the columns "
                "(e.g. `select(lambda t: (t.id, t.amount))`), got "
                f"{len(selectors)} arguments"
            )
        selector = selectors[0]
        if not callable(selector):
            raise TypeError(
                "select() expected a selector callable or column-name strings "
                f"(e.g. `lambda t: (t.id, t.amount)`), got {type(selector).__name__}"
            )
        # Strings were dispatched above, so the lone selector is the lambda
        # form; the tuple element type is too wide for the checker to see it.
        fields = _resolve_projection_selector(
            cast("RowSelector[T]", selector), self.model_cls
        )
        return ProjectedQuery(self, fields)

    def order_by(
        self,
        field: "str | Callable[[QueryProxy[T]], FieldProxy[Any]]",
        direction: str = "asc",
        *,
        nulls: str | None = None,
    ) -> Self:
        """Sort the results by a column, after any sort keys added before.

        Name the column with a lambda, the documented form, or a string. Only
        the lambda can sort by a related model's column
        (`lambda post: post.author.name`), which joins that model the way
        `where()` does; a string always names a column of the model itself.
        See [Ordering, limit & offset](../guide/queries.md#ordering-limit-offset).

        Args:
            field: A lambda returning the column, such as
                `lambda post: post.views`, or the column's name.
            direction: `"asc"` (the default) or `"desc"`.
            nulls: Where `NULL` values sort: `"first"`, `"last"` or
                `"native"`, the database's own rule. Defaults to `None`,
                which sorts them last on every database.

        Returns:
            A new query with the sort key added.

        Raises:
            AttributeError: The column is not one the model has.
            TypeError: The lambda returns a relation or anything else that is
                not a column.
            ValueError: `direction` or `nulls` has a value not listed above.

        Examples:
            ```python {test=run prelude=reference}
            posts = await Post.select().order_by(lambda post: post.views, "desc").all()
            assert [post.views for post in posts] == [1843, 952]
            ```
        """
        if direction.lower() not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        nulls_norm = _normalize_order_by_nulls(nulls)

        path: tuple[str, ...] = ()
        if isinstance(field, str):
            col_name = validate_query_column(self.model_cls, field)
        elif callable(field):
            selected = field(QueryProxy(self.model_cls))
            # Ordering by a bare relation (no column selected) is meaningless —
            # reject it loudly rather than emitting an order_by the SELECT
            # walker cannot render. Ordering by a related COLUMN (a FieldProxy
            # with a non-empty path) is valid traversal (#271).
            if isinstance(selected, RelationProxy):
                relation = selected._path[-1]
                raise TypeError(
                    f"order_by() selector returned the bare relation {relation!r}, "
                    "not a column; order by a column on it instead "
                    f"(e.g. t.{relation}.<column>)."
                )
            if isinstance(selected, ValueExpr):
                selected._reject_clause("order_by")
            if not isinstance(selected, FieldProxy):
                raise TypeError(
                    "order_by() selector must return a FieldProxy "
                    f"(e.g. `lambda u: u.created_at`), got {type(selected).__name__}"
                )
            col_name = selected.column
            path = selected.path
        else:
            raise TypeError(
                "order_by() expected a column-name string or a lambda selector, "
                f"got {type(field).__name__}"
            )

        new = self._clone()
        new.order_by_clause.append(
            OrderByEntry(
                column=col_name,
                direction=direction.lower(),
                path=path,
                nulls=nulls_norm,
            )
        )
        if path:
            new._joins.setdefault(path, "inner")
        return new

    def join(self, selector: "Callable[[QueryProxy[T]], Any]") -> Self:
        """Join a related model with `INNER JOIN`, keeping only rows that have it.

        The lambda names a foreign key relation, not a column on it. On a
        nullable relation, a `join()` with no condition drops the rows whose
        relation is empty. A `where()` or `order_by()` that reaches into the
        same relation shares the join. See
        [Join type for traversed relations](../guide/queries.md#join-type-for-traversed-relations).

        Args:
            selector: A lambda returning a relation, such as
                `lambda post: post.author`, or a longer path through several.

        Returns:
            A new query with the join added.

        Raises:
            TypeError: `selector` is not callable, or names a column or a
                reverse relation instead of a foreign key relation.
            ValueError: A [`left_join`][ferro.query.builder.Query.left_join]
                already marked part of the path.

        Examples:
            ```python {test=run prelude=reference}
            query = Post.select().join(lambda post: post.author)
            assert await query.count() == 2
            ```
        """
        return self._add_explicit_join(selector, "inner")

    def left_join(self, selector: "Callable[[QueryProxy[T]], Any]") -> Self:
        """Join a related model with `LEFT JOIN`, keeping rows that lack it.

        Every relation along the path is joined with `LEFT JOIN`, so a row
        missing the relation at any step is still returned, and a field read
        through the relation is `None`. The `LEFT JOIN` also applies to a
        `where()`, `order_by()` or `select()` that reaches into the same
        path. See
        [Keeping rows that have no relation](../guide/queries.md#keeping-rows-that-have-no-relation).

        Args:
            selector: A lambda returning a relation, such as
                `lambda post: post.author`, or a longer path through several.

        Returns:
            A new query with the join added.

        Raises:
            TypeError: `selector` is not callable, or names a column or a
                reverse relation instead of a foreign key relation.
            ValueError: A [`join`][ferro.query.builder.Query.join] already
                marked part of the path.

        Examples:
            ```python {test=run prelude=reference}
            rows = await (
                Post.select(lambda post: {"post": post.title, "author": post.author.name})
                .left_join(lambda post: post.author)
                .order_by(lambda post: post.id)
                .all()
            )
            assert [row.author for row in rows] == ["Ada Lovelace", "Grace Hopper"]
            ```
        """
        return self._add_explicit_join(selector, "left")

    def include(self, selector: "Callable[[QueryProxy[T]], Any]") -> Self:
        """Load a related model with each result, in the same query.

        Each returned instance has the relation filled in: reading it is a
        plain attribute that holds the related instance, with no `await`
        and no second query. `include()` never changes which rows come back
        or how many. A path through several relations fills in each one. See
        [Populating relations with include()](../guide/queries.md#populating-relations-with-include).

        Args:
            selector: A lambda returning a foreign key relation, such as
                `lambda post: post.author`.

        Returns:
            A new query that loads the relation.

        Raises:
            TypeError: `selector` is a string or not callable, or names a
                column or a reverse or many-to-many relation.

        Examples:
            ```python {test=run prelude=reference}
            posts = await Post.select().include(lambda post: post.author).order_by(lambda post: post.id).all()
            assert posts[0].author.name == "Ada Lovelace"
            ```
        """
        path = _resolve_include_selector(selector, self.model_cls)
        new = self._clone()
        new._includes.setdefault(path, None)
        return new

    def _add_explicit_join(
        self, selector: "Callable[[QueryProxy[T]], Any]", join_type: str
    ) -> Self:
        """Shared body of `join`/`left_join` (#272).

        Resolves the selector to a relation path, checks every edge for a
        contradictory explicit mark (INNER vs LEFT), then records the marks and
        registers the full path so it renders. Re-marking an edge the same
        direction is idempotent.
        """
        path = _resolve_join_selector(selector, self.model_cls)
        edges = path_edges(path)
        new = self._clone()
        for edge in edges:
            existing = new._explicit_edges.get(edge)
            if existing is not None and existing != join_type:
                relation_path = ".".join(edge)
                raise ValueError(
                    f"conflicting explicit join types on relation edge "
                    f"{relation_path!r}: already marked {existing!r} by a prior "
                    f"join()/left_join(), cannot re-mark {join_type!r}. Use one "
                    "join type per edge."
                )
        for edge in edges:
            new._explicit_edges[edge] = join_type
        new._joins.setdefault(path, join_type)
        return new

    def limit(self, value: int) -> Self:
        """Return at most `value` rows.

        Args:
            value: The largest number of rows to return.

        Returns:
            A new query with the limit set.

        Examples:
            ```python {test=run prelude=reference}
            posts = await Post.select().order_by(lambda post: post.id).limit(1).all()
            assert [post.id for post in posts] == [1]
            ```
        """
        new = self._clone()
        new._limit = value
        return new

    def offset(self, value: int) -> Self:
        """Skip the first `value` rows.

        For pages deep into a large table, [`after`][ferro.query.builder.Query.after]
        stays fast where an offset grows slower with every skipped row. See
        [Pagination](../howto/pagination.md).

        Args:
            value: The number of rows to skip.

        Returns:
            A new query with the offset set.

        Raises:
            ValueError: `after()` or `before()` is already set; a query has
                one starting point.

        Examples:
            ```python {test=run prelude=reference}
            posts = await Post.select().order_by(lambda post: post.id).limit(10).offset(1).all()
            assert [post.id for post in posts] == [2]
            ```
        """
        if self._after is not None or self._before is not None:
            raise ValueError(
                "after()/before() cannot be combined with offset(): a query "
                "has one start (an offset or a position bound, never both)."
            )
        new = self._clone()
        new._offset = value
        return new

    def _assert_position_order_keys(self) -> None:
        """Require order keys that include the root primary key (#393/#396).

        Root and traversed columns are both legal. A related column named
        ``id`` is not the model's primary key — only a root PK slot counts.
        Aggregate output names are rejected even when the PK is also a
        group key.
        """
        pk = getattr(self.model_cls, "__ferro_pk__", None)
        if pk is None:
            raise ValueError(
                f"{self.model_cls.__name__} has no primary-key column, and "
                "after()/before()/position_of() require one."
            )
        if not self.order_by_clause:
            raise ValueError(
                "after()/before() require order_by() keys that include the "
                "model's primary key"
            )
        pk_seen = False
        for entry in self.order_by_clause:
            if not entry.path and entry.column == pk:
                pk_seen = True
        if not pk_seen:
            raise ValueError(
                "after()/before() require the model's primary key in the "
                "order keys; Ferro will not append it silently"
            )
        projection = getattr(self, "_projection", None)
        if projection:
            agg_names = {field.name for field in projection if field.agg}
            for entry in self.order_by_clause:
                if not entry.path and entry.column in agg_names:
                    raise ValueError(
                        "after()/before()/position_of() do not support "
                        f"aggregate order key {entry.column!r}"
                    )

    def position_of(self, row: T) -> tuple[Any, ...]:
        """Read an instance's values for this query's sort keys.

        The tuple is the instance's place in the query's order, the cursor
        that [`after`][ferro.query.builder.Query.after] and
        [`before`][ferro.query.builder.Query.before] take. Store it to resume
        paging later, for example in an API's next-page token.

        Args:
            row: An instance of the queried model.

        Returns:
            One value per sort key, in the order the keys were added.

        Raises:
            TypeError: `row` is not an instance of the queried model.
            ValueError: The sort keys do not include the primary key, or a
                key reaches into a relation the instance does not have loaded
                with `include()`.

        Examples:
            ```python {test=run prelude=reference}
            by_views = Post.select().order_by(lambda post: post.views, "desc").order_by(lambda post: post.id)
            top = await by_views.first()
            assert by_views.position_of(top) == (1843, 1)
            ```
        """
        if not isinstance(row, self.model_cls):
            raise TypeError(
                "position_of() expected an instance of "
                f"{self.model_cls.__name__}, got {type(row).__name__}"
            )
        self._assert_position_order_keys()
        return tuple(
            _read_instance_order_value(row, entry) for entry in self.order_by_clause
        )

    def _resolve_position(
        self, position: tuple[Any, ...] | T, *, op: str
    ) -> tuple[Any, ...]:
        """Validate a position bound for ``after()`` / ``before()``."""
        if self._offset is not None:
            raise ValueError(
                f"{op}() cannot be combined with offset(): a query has one "
                "start (an offset or a position bound, never both)."
            )
        if op == "after" and self._before is not None:
            raise ValueError(
                "after() cannot be combined with before(): a query has one start."
            )
        if op == "before" and self._after is not None:
            raise ValueError(
                "after() cannot be combined with before(): a query has one start."
            )
        if isinstance(position, tuple):
            self._assert_position_order_keys()
            expected = len(self.order_by_clause)
            if len(position) != expected:
                raise ValueError(
                    f"{op}() expected {expected} position values "
                    f"(one per order_by key), got {len(position)}"
                )
            bound = position
        elif isinstance(position, (self.model_cls, Row)):
            bound = self.position_of(position)
        else:
            raise TypeError(
                f"{op}() expected a position tuple or a model instance, "
                f"got {type(position).__name__}"
            )
        pk = getattr(self.model_cls, "__ferro_pk__", None)
        for entry, value in zip(self.order_by_clause, bound, strict=True):
            if not entry.path and entry.column == pk and value is None:
                raise ValueError(
                    f"{op}() does not support None in the primary-key position slot"
                )
        return bound

    def after(self, position: tuple[Any, ...] | T) -> Self:
        """Start the results just after a position in the query's order.

        This is keyset paging: pass the last row of one page to get the
        next. The position is excluded, and rows inserted or deleted
        elsewhere do not shift the page. The sort keys must include the
        primary key, so that no two rows share a position. See
        [Keyset (cursor) pagination](../howto/pagination.md#keyset-cursor-pagination).

        Args:
            position: An instance of the queried model, or the tuple of its
                sort-key values that
                [`position_of`][ferro.query.builder.Query.position_of]
                returns. Any value but the primary key's may be `None`.

        Returns:
            A new query that starts after `position`.

        Raises:
            TypeError: `position` is neither a tuple nor an instance of the
                queried model.
            ValueError: The sort keys do not include the primary key, the
                tuple has the wrong length, its primary key value is `None`,
                or `offset()` or `before()` is already set.

        Examples:
            ```python {test=run prelude=reference}
            by_views = Post.select().order_by(lambda post: post.views, "desc").order_by(lambda post: post.id).limit(1)

            page = await by_views.all()
            assert [post.views for post in page] == [1843]

            next_page = await by_views.after(page[-1]).all()
            assert [post.views for post in next_page] == [952]
            ```
        """
        bound = self._resolve_position(position, op="after")
        new = self._clone()
        new._after = bound
        return new

    def before(self, position: tuple[Any, ...] | T) -> Self:
        """End the results just before a position in the query's order.

        With a limit, this is the previous page, still in the query's order.
        Without one, it is every row before the position. The position is
        excluded, and the sort keys must include the primary key, as for
        [`after`][ferro.query.builder.Query.after].

        Args:
            position: An instance of the queried model, or the tuple of its
                sort-key values that
                [`position_of`][ferro.query.builder.Query.position_of]
                returns. Any value but the primary key's may be `None`.

        Returns:
            A new query that ends before `position`.

        Raises:
            TypeError: `position` is neither a tuple nor an instance of the
                queried model.
            ValueError: The sort keys do not include the primary key, the
                tuple has the wrong length, its primary key value is `None`,
                or `offset()` or `after()` is already set.

        Note:
            Without a limit, [`first`][ferro.query.builder.Query.first]
            returns the row just before the position, while `all()[0]` is
            the first row of the query.

        Examples:
            ```python {test=run prelude=reference}
            by_views = Post.select().order_by(lambda post: post.views, "desc").order_by(lambda post: post.id).limit(1)

            previous_page = await by_views.before((952, 2)).all()
            assert [post.views for post in previous_page] == [1843]
            ```
        """
        bound = self._resolve_position(position, op="before")
        new = self._clone()
        new._before = bound
        return new

    @terminal
    async def all(self: "Query[T]") -> list[T]:
        """Run the query and return every matching record.

        Returns:
            The matching instances, in the query's order.

        Examples:
            ```python {test=run prelude=reference}
            posts = await Post.where(lambda post: post.views > 1000).all()
            assert [post.title for post in posts] == ["Notes on the Analytical Engine"]
            ```
        """
        compiled = compile_query(self, "fetch")
        route = await self._transaction_or_using()
        rows = await send(fetch_filtered, render_fetch)(
            self.model_cls,
            compiled.wire_json,
            route,
            hop_classes=compiled.hop_classes,
            secret_values=compiled.secret_values,
        )
        if self._before is not None:
            rows.reverse()
        return rows

    @terminal
    async def count(self: "Query[T]") -> int:
        """Count the matching rows.

        Limits and offsets are ignored: the count covers every row the
        conditions match.

        Returns:
            The number of matching rows.

        Examples:
            ```python {test=run prelude=reference}
            assert await Post.where(lambda post: post.views > 900).count() == 2
            ```
        """
        return await self._count("count")

    async def _count(self, operation: Literal["count", "exists"]) -> int:
        """Count the matching rows, naming the statement-log records `operation`."""
        compiled = compile_query(self, "count")
        route = await self._transaction_or_using()
        return await send(count_filtered, render_count)(
            model_identity(self.model_cls),
            compiled.wire_json,
            route,
            secret_values=compiled.secret_values,
            operation=operation,
        )

    @_update_terminal
    async def update(
        self: "Query[T]",
        recipe: Callable[[QueryProxy[T]], dict[str, Any]] | None = None,
        /,
        **fields: Any,
    ) -> int:
        """Update every matching row in one statement.

        Pass the new values as keywords, or pass one lambda, a recipe, that
        returns a dict of new values computed in the database. A recipe
        value is a literal, another column of the row, `+` or `-` arithmetic
        on columns, [`now`][ferro.now] for the database's clock, or
        `.merge()` on a JSON column (Postgres only). `NULL + 1` is `NULL`.
        The two forms cannot be mixed. See
        [Expression updates](../guide/mutations.md#expression-updates).

        Args:
            recipe: An optional recipe: a lambda that takes the row stand-in
                and returns a dict of column names to new values. It is
                positional only, so `recipe=` as a keyword still names a
                column.
            **fields: Column names and the literal values to write.

        Returns:
            The number of rows updated.

        Raises:
            TypeError: Both forms are given, the recipe is not callable or
                does not return a dict, or a keyword value is not a literal.
            ValueError: The recipe assigns nothing, the query has a
                `limit()`, `offset()`, `after()` or `before()`, or it reaches
                into a related model.

        Examples:
            Keywords write literal values:

            ```python {test=run prelude=reference}
            updated = await Post.where(lambda post: post.id == 2).update(published=False)
            assert updated == 1
            ```

            A recipe computes each row's new value from its current one:

            ```python {test=run prelude=reference}
            await Post.where(lambda post: post.views > 900).update(lambda post: {"views": post.views + 1})
            posts = await Post.select().order_by(lambda post: post.id).all()
            assert [post.views for post in posts] == [1844, 953]
            ```
        """
        if recipe is not None and fields:
            raise TypeError(
                "update() accepts either a recipe callable or keyword "
                "literals, not both"
            )
        if recipe is not None:
            compiled = compile_query(
                self, "update", recipe=_resolve_update_recipe(recipe, self.model_cls)
            )
        else:
            compiled = compile_query(self, "update", assignments=fields)
        route = await self._transaction_or_using()
        return await send(update_filtered, render_update)(
            model_identity(self.model_cls),
            compiled.wire_json,
            route,
            secret_values=compiled.secret_values,
        )

    @terminal
    async def first(self: "Query[T]") -> T | None:
        """Run the query and return its first record, or `None` when nothing matches.

        Returns:
            The first matching instance in the query's order, or `None`.

        Examples:
            ```python {test=run prelude=reference}
            post = await Post.select().order_by(lambda post: post.views).first()
            assert post.title == "The Education of a Computer"
            ```
        """
        results = await self.limit(1).all()
        return results[0] if results else None

    @terminal
    async def delete(self: "Query[T]") -> int:
        """Delete every matching row in one statement.

        Returns:
            The number of rows deleted.

        Raises:
            ValueError: The query has a `limit()`, `offset()`, `after()` or
                `before()`, or reaches into a related model through
                `where()`, `join()` or `left_join()`. Filter on the model's own columns instead,
                such as the foreign key column `author_id`.

        Examples:
            ```python {test=run prelude=reference}
            deleted = await Post.where(lambda post: post.views < 1000).delete()
            assert deleted == 1
            ```
        """
        compiled = compile_query(self, "delete")
        route = await self._transaction_or_using()
        return await send(delete_filtered, render_delete)(
            model_identity(self.model_cls),
            compiled.wire_json,
            route,
            secret_values=compiled.secret_values,
        )

    @terminal
    async def exists(self: "Query[T]") -> bool:
        """Return whether any row matches.

        Returns:
            `True` when at least one row matches, otherwise `False`.

        Examples:
            ```python {test=run prelude=reference}
            assert await Author.where(lambda author: author.email == "ada@example.com").exists()
            ```
        """
        return await self._count("exists") > 0

    async def add(self, *instances: Any) -> None:
        """Link instances to the owner of a many-to-many relation.

        Call it on the relation, as in `await post.tags.add(history, science)`.
        See [Many-to-many](../guide/relationships.md#many-to-many).

        Args:
            *instances: The related instances to link. Each is identified by
                its `id` field, so the related model's primary key must be
                named `id`.

        Raises:
            RuntimeError: The query is not a many-to-many relation.
        """
        if not self._m2m_context:
            raise RuntimeError(
                "'.add()' can only be used on Many-to-Many relationships"
            )

        ids = []
        for inst in instances:
            # Assume 'id' for now
            ids.append(getattr(inst, "id"))

        route = await self._transaction_or_using()
        await add_m2m_links(
            self._m2m_context.join_table,
            self._m2m_context.source_col,
            self._m2m_context.target_col,
            self._m2m_context.source_id,
            ids,
            route,
        )

    async def remove(self, *instances: Any) -> None:
        """Unlink instances from the owner of a many-to-many relation.

        Call it on the relation, as in `await post.tags.remove(history)`. The
        instances' rows stay; only the links go.

        Args:
            *instances: The related instances to unlink. Each is identified
                by its `id` field, so the related model's primary key must be
                named `id`.

        Raises:
            RuntimeError: The query is not a many-to-many relation.
        """
        if not self._m2m_context:
            raise RuntimeError(
                "'.remove()' can only be used on Many-to-Many relationships"
            )

        ids = []
        for inst in instances:
            ids.append(getattr(inst, "id"))

        route = await self._transaction_or_using()
        await remove_m2m_links(
            self._m2m_context.join_table,
            self._m2m_context.source_col,
            self._m2m_context.target_col,
            self._m2m_context.source_id,
            ids,
            route,
        )

    async def clear(self) -> None:
        """Unlink every instance from the owner of a many-to-many relation.

        Call it on the relation, as in `await post.tags.clear()`. The related
        rows stay; only the links go.

        Raises:
            RuntimeError: The query is not a many-to-many relation.
        """
        if not self._m2m_context:
            raise RuntimeError(
                "'.clear()' can only be used on Many-to-Many relationships"
            )

        route = await self._transaction_or_using()
        await clear_m2m_links(
            self._m2m_context.join_table,
            self._m2m_context.source_col,
            self._m2m_context.source_id,
            route,
        )

    def __repr__(self):
        """Show the model and the conditions, for debugging."""
        return f"<Query model={self.model_cls.__name__} where={self.where_clause}>"


class ProjectedQuery(Query[T]):
    """A query that returns some columns as records, not model instances.

    [`select`][ferro.query.builder.Query.select] with a lambda or column names
    returns one. Awaiting [`all`][ferro.query.builder.ProjectedQuery.all]
    gives [`Row`][ferro.query.Row] records in a [`Rows`][ferro.query.Rows]
    list. Records are plain values: they are not tracked by the session and
    cannot be saved. Filtering, sorting and paging work as on any query.

    A dict selector whose values include an aggregate such as
    `post.views.sum()` groups the rows by the plain fields beside it. See
    [Aggregations and grouped queries](../guide/aggregations.md).

    Examples:
        ```python {test=run prelude=reference}
        rows = await Post.select(
            lambda post: {"author": post.author.name, "total_views": post.views.sum()}
        ).order_by("author").all()
        assert [(row.author, row.total_views) for row in rows] == [
            ("Ada Lovelace", 1843),
            ("Grace Hopper", 952),
        ]
        ```
    """

    def __init__(self, source: Query[T], fields: tuple[_ProjectedField, ...]) -> None:
        """Project a query to the given fields.

        Args:
            source: The query to project. It is left unchanged.
            fields: The fields to return, in order.

        Raises:
            ValueError: A sort key added before the projection is not one of
                its group keys.
        """
        # Copies the source's state through ``_clone()`` (fresh mutable
        # containers). Every traversed field's relation path registers a join:
        # projection traversal narrows like where()/order_by() traversal,
        # sharing one join per relation path, and an explicit left_join()
        # still wins through the edge marks.
        self.__dict__.update(source._clone().__dict__)
        # Immutable, so chained clones share it safely.
        self._projection: tuple[_ProjectedField, ...] = fields
        for field in fields:
            if field.path:
                self._joins.setdefault(field.path, "inner")
        # Rule 3 (#295) covers sort keys added BEFORE the projection too:
        # an order_by() chained ahead of an aggregate select() must already
        # name a group key (or coincide with an output field) — checked here
        # so the error stays at build time, not at the render boundary.
        if self._has_aggregate():
            output_names = {field.name for field in fields}
            for order in self.order_by_clause:
                column, path = order.column, order.path
                if not path and column in output_names:
                    continue
                if self._is_group_key_source(column, path):
                    continue
                dotted = "t." + ".".join((*path, column))
                raise ValueError(
                    f"order_by() key {dotted} (added before this select()) "
                    "is not a group key of the aggregate projection: each "
                    "group would answer with an arbitrary row's value. "
                    "Project it as a group key, or sort by an output field "
                    "after the select()."
                )

    def _has_aggregate(self) -> bool:
        """True when this is an AGGREGATE PROJECTION (GLOSSARY.md): at least
        one aggregate field, so every plain field is a group key (#295)."""
        return any(field.agg for field in self._projection)

    def _append_order_by(
        self,
        column: str,
        direction: str,
        path: tuple[str, ...],
        *,
        nulls: str,
    ) -> Self:
        """Clone with one resolved ORDER BY entry appended (#295)."""
        new = self._clone()
        new.order_by_clause.append(
            OrderByEntry(
                column=column,
                direction=direction.lower(),
                path=path,
                nulls=nulls,
            )
        )
        if path:
            new._joins.setdefault(path, "inner")
        return new

    def order_by(
        self,
        field: "str | Callable[[QueryProxy[T]], Any]",
        direction: str = "asc",
        *,
        nulls: str | None = None,
    ) -> Self:
        """Sort the records by an output field, a column or an aggregate.

        A string names an output field first and a column of the model
        second, the way SQL's `ORDER BY` reads names. A lambda names a
        column, which may reach into a related model, or an aggregate that
        the projection returns, such as `lambda post: post.views.sum()`.

        When the projection has an aggregate, every sort key must be a
        group key or an aggregate: any other column has no single value per
        group. See
        [Ordering grouped results](../guide/aggregations.md#ordering-grouped-results).

        Args:
            field: An output field or column name, or a lambda returning a
                column or an aggregate.
            direction: `"asc"` (the default) or `"desc"`.
            nulls: Where `NULL` values sort: `"first"`, `"last"` or
                `"native"`, the database's own rule. Defaults to `None`,
                which sorts them last on every database.

        Returns:
            A new query with the sort key added.

        Raises:
            ValueError: `direction` or `nulls` has a value not listed above,
                or on an aggregate projection the key is neither a group key
                nor an aggregate the projection returns.
            AttributeError: A string names neither an output field nor a
                column of the model.
            TypeError: The lambda returns a relation or anything else that is
                not a column or an aggregate.

        Examples:
            ```python {test=run prelude=reference}
            rows = await Post.select(
                lambda post: {"author": post.author.name, "total_views": post.views.sum()}
            ).order_by(lambda post: post.views.sum(), "desc").all()
            assert [row.author for row in rows] == ["Ada Lovelace", "Grace Hopper"]
            ```
        """
        if direction.lower() not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        nulls_norm = _normalize_order_by_nulls(nulls)
        has_aggregate = self._has_aggregate()
        output_names = {f.name for f in self._projection}

        if isinstance(field, str):
            # Rule 1: output field names first (group keys and aggregates
            # both sort by name), then root columns.
            if field in output_names:
                return self._append_order_by(field, direction, (), nulls=nulls_norm)
            try:
                validate_query_column(self.model_cls, field)
            except AttributeError as exc:
                raise AttributeError(
                    f"{exc.args[0]} On this projected query, output field "
                    f"names also sort: {', '.join(sorted(output_names))}.",
                    name=getattr(exc, "name", None),
                    obj=getattr(exc, "obj", None),
                ) from None
            if has_aggregate and not self._is_group_key_source(field, ()):
                raise ValueError(
                    f"order_by({field!r}) on an aggregate projection must "
                    "name a group key or an aggregate: it is neither an "
                    "output field nor a group key's source column, so each "
                    "group would answer with an arbitrary row's value. Sort "
                    "by an output field name, or project the column as a "
                    "group key."
                )
            return self._append_order_by(field, direction, (), nulls=nulls_norm)

        if not callable(field):
            raise TypeError(
                "order_by() expected a column-name string or a lambda "
                f"selector, got {type(field).__name__}"
            )
        selected = field(QueryProxy(self.model_cls))
        # Rule 2: the lambda form spells source expressions — aggregates
        # resolve to the projected field carrying the same expression and
        # sort by its output name.
        if isinstance(selected, AggregateExpr):
            for proj in self._projection:
                if (
                    proj.agg == selected.fn
                    and proj.column == selected.column
                    and proj.path == selected.path
                ):
                    return self._append_order_by(
                        proj.name, direction, (), nulls=nulls_norm
                    )
            raise ValueError(
                f"order_by() aggregate {selected._dotted()} matches no "
                "projected field; project it to sort by it (e.g. "
                f'select(lambda t: {{..., "key": {selected._dotted()}}}) '
                'with order_by("key")).'
            )
        if isinstance(selected, RelationProxy):
            relation = selected._path[-1]
            raise TypeError(
                f"order_by() selector returned the bare relation {relation!r}, "
                "not a column; order by a column on it instead "
                f"(e.g. t.{relation}.<column>)."
            )
        if isinstance(selected, ValueExpr):
            selected._reject_clause("order_by")
        if not isinstance(selected, FieldProxy):
            raise TypeError(
                "order_by() selector must return a FieldProxy "
                f"(e.g. `lambda u: u.created_at`), got {type(selected).__name__}"
            )
        # Rule 3: on an aggregate projection a source column must be a group
        # key; on a plain projection unselected columns stay sortable.
        if has_aggregate and not self._is_group_key_source(
            selected.column, selected.path
        ):
            dotted = "t." + ".".join((*selected.path, selected.column))
            raise ValueError(
                f"order_by() key {dotted} on an aggregate projection must be "
                "a group key or an aggregate: it is not a group key's source "
                "column, so each group would answer with an arbitrary row's "
                "value. Project it as a group key, or sort by an aggregate."
            )
        return self._append_order_by(
            selected.column, direction, selected.path, nulls=nulls_norm
        )

    def _is_group_key_source(self, column: str, path: tuple[str, ...]) -> bool:
        """True when ``(column, path)`` is some plain field's source (#295)."""
        return any(
            field.agg is None and field.column == column and field.path == path
            for field in self._projection
        )

    @terminal
    def count(self: "ProjectedQuery[T]") -> Awaitable[int]:
        """Count the matching rows, whatever columns the projection selects.

        On a projection with an aggregate, "count" could mean rows or
        groups, so the call raises before any SQL runs.

        Returns:
            (int): The number of matching rows, once awaited.

        Raises:
            ValueError: The projection has an aggregate. Count rows with the
                query before `select()`, or groups with `len(await query.all())`.

        Examples:
            ```python {test=run prelude=reference}
            assert await Post.select(lambda post: post.title).count() == 2
            ```
        """
        if self._has_aggregate():
            raise ValueError(
                "count() on an aggregate projection is ambiguous: count the "
                "matching rows with an unprojected query "
                "(Model.where(...).count()), or count the groups with "
                "len(await q.all())."
            )
        return super().count()

    @terminal
    def exists(self: "ProjectedQuery[T]") -> Awaitable[bool]:
        """Return whether any row matches, whatever columns the projection selects.

        On a projection with only aggregates there is always exactly one
        record, so the call raises before any SQL runs.

        Returns:
            (bool): `True` when at least one row matches, once awaited.

        Raises:
            ValueError: The projection has an aggregate. Test rows with the
                query before `select()`, or groups with
                `len(await query.all()) > 0`.
        """
        if self._has_aggregate():
            raise ValueError(
                "exists() on an aggregate projection is ambiguous: an "
                "aggregate-only projection always yields exactly one record. "
                "Test row existence with an unprojected query "
                "(Model.where(...).exists()), or group existence with "
                "len(await q.all()) > 0."
            )
        return super().exists()

    def position_of(self, row: T | Row) -> tuple[Any, ...]:
        """Read a record's values for this query's sort keys.

        Every sort key must be among the selected fields, under its own name
        or an output name. Otherwise build the tuple yourself for `after()`
        or `before()`.

        Args:
            row: A record this query returned, or an instance of the queried
                model.

        Returns:
            One value per sort key, in the order the keys were added.

        Raises:
            TypeError: `row` is neither a [`Row`][ferro.query.Row] nor an
                instance of the queried model.
            ValueError: A sort key is not selected, the sort keys do not
                include the primary key, or a key reaches into a relation the
                instance does not have loaded.

        Examples:
            ```python {test=run prelude=reference}
            by_views = Post.select(lambda post: (post.id, post.views)).order_by(lambda post: post.views, "desc").order_by(lambda post: post.id)
            top = await by_views.first()
            assert by_views.position_of(top) == (1843, 1)
            ```
        """
        if isinstance(row, Row):
            self._assert_position_order_keys()
            return tuple(
                self._read_projected_order_value(row, entry)
                for entry in self.order_by_clause
            )
        return super().position_of(row)

    def _projected_name_for_order_key(self, entry: OrderByEntry) -> str | None:
        """Output field name that carries this order key, or ``None``."""
        name_match: str | None = None
        for field in self._projection:
            if field.agg:
                continue
            if field.column == entry.column and field.path == entry.path:
                return field.name
            if not entry.path and field.name == entry.column:
                name_match = field.name
        return name_match

    def _read_projected_order_value(self, row: Row, entry: OrderByEntry) -> Any:
        name = self._projected_name_for_order_key(entry)
        if name is None:
            dotted = _dotted_order_key(entry)
            raise ValueError(
                f"position_of() cannot read order key {dotted!r} from the "
                "projection; select it, or pass a tuple"
            )
        return getattr(row, name)

    @terminal
    async def all(self: "ProjectedQuery[T]") -> Rows[Row]:  # type: ignore[override]
        """Run the query and return a record for every matching row, or group.

        Returns:
            A [`Rows`][ferro.query.Rows] list of [`Row`][ferro.query.Row]
                records, each with the selected fields in selection order.

        Examples:
            ```python {test=run prelude=reference}
            rows = await Post.select(lambda post: post.views).order_by(lambda post: post.id).all()
            assert [row.views for row in rows] == [1843, 952]
            ```
        """
        compiled = compile_query(self, "fetch")
        route = await self._transaction_or_using()
        records = await send(fetch_filtered, render_fetch)(
            self.model_cls,
            compiled.wire_json,
            route,
            record_cls=Row,
            hop_classes=compiled.hop_classes,
            secret_values=compiled.secret_values,
        )
        if self._before is not None:
            records.reverse()
        return _ROWS_OF_ROW._wrap(records)

    @terminal
    async def first(self: "ProjectedQuery[T]") -> Row | None:  # type: ignore[override]
        """Run the query and return its first record, or `None` when nothing matches.

        Returns:
            The first [`Row`][ferro.query.Row] in the query's order, or `None`.

        Examples:
            ```python {test=run prelude=reference}
            row = await Post.select(lambda post: post.title).order_by(lambda post: post.views).first()
            assert row.title == "The Education of a Computer"
            ```
        """
        results = await self.limit(1).all()
        return results[0] if results else None

    def select(self, *selectors: "RowSelector[T] | str") -> NoReturn:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        """Refuse a second `select()`, which would change the result type mid-chain.

        Args:
            *selectors: Ignored; the call always raises.

        Raises:
            ValueError: Always. Select every field in one `select()` call, or
                start a new query from the model.
        """
        raise ValueError(
            "select() was already applied to this query; replacing a "
            "projection changes the result type mid-chain. Name every "
            "projected column in one select() call, or start a new query "
            "from the model."
        )

    def include(self: Never, selector: Any) -> NoReturn:  # type: ignore[override]
        """Refuse `include()`: a record is flat and cannot hold a related instance.

        A type checker reports the call as an error too. To read a related
        model's columns, select them through the relation
        (`lambda post: {"author": post.author.name}`).

        Args:
            selector: Ignored; the call always raises.

        Raises:
            ValueError: Always. Select the related columns, or call
                `include()` on a query without `select()`.
        """
        raise ValueError(
            "include() cannot be combined with a projection: a query carries "
            "exactly one materialization plan — projected records or "
            "populated instances, never both, and record results are flat "
            "(ADR-0009). Reach across the relation with traversed projection "
            'instead (e.g. select(lambda t: {"account_name": '
            "t.account.name})), or populate instances with include() on an "
            "unprojected query."
        )

    # `self: Never` makes mutating a projected query a STATIC error at the
    # call site (pinned by tests/static_fixtures/bad_projections.py) while the
    # runtime bodies raise at build time — synchronously, at the call, before
    # any coroutine or SQL exists (#280). A projection is a read shape;
    # silently ignoring it would make select(...) a no-op on mutations.

    def update(  # type: ignore[override]
        self: Never,
        recipe: Callable[..., Any] | None = None,
        /,
        **fields: Any,
    ) -> NoReturn:
        """Refuse `update()`: a projection only reads.

        A type checker reports the call as an error too.

        Args:
            recipe: Ignored; the call always raises.
            **fields: Ignored; the call always raises.

        Raises:
            ValueError: Always. Update through a query without `select()`,
                such as `Model.where(...).update(...)`.
        """
        raise ValueError(
            "update() is not supported on a projected query: a projection is "
            "a read shape. Build the mutation from the model instead "
            "(e.g. Model.where(...).update(...))."
        )

    def delete(self: Never) -> NoReturn:  # type: ignore[override]
        """Refuse `delete()`: a projection only reads.

        A type checker reports the call as an error too.

        Raises:
            ValueError: Always. Delete through a query without `select()`,
                such as `Model.where(...).delete()`.
        """
        raise ValueError(
            "delete() is not supported on a projected query: a projection is "
            "a read shape. Build the mutation from the model instead "
            "(e.g. Model.where(...).delete())."
        )

    def __repr__(self):
        """Show the model, the selected fields and the conditions, for debugging."""
        return (
            f"<ProjectedQuery model={self.model_cls.__name__} "
            f"fields={[field.name for field in self._projection]} "
            f"where={self.where_clause}>"
        )


if TYPE_CHECKING:

    class _RelationAll:
        """`Relation.all` as a type checker reads it: its rows are instances
        of `E` whether the relation is written `Relation[list[E]]` or
        `Relation[E]`."""

        @overload
        def __get__(self, query: None, owner: Any) -> "_RelationAll": ...
        @overload
        def __get__(
            self, query: "Relation[list[E]]", owner: Any
        ) -> Terminal[[], list[E]]: ...
        @overload
        def __get__(
            self, query: "Relation[E]", owner: Any
        ) -> Terminal[[], list[E]]: ...
        def __get__(self, query: Any, owner: Any) -> Any: ...

    class _RelationFirst:
        """`Relation.first` as a type checker reads it (see `_RelationAll`)."""

        @overload
        def __get__(self, query: None, owner: Any) -> "_RelationFirst": ...
        @overload
        def __get__(
            self, query: "Relation[list[E]]", owner: Any
        ) -> Terminal[[], E | None]: ...
        @overload
        def __get__(
            self, query: "Relation[E]", owner: Any
        ) -> Terminal[[], E | None]: ...
        def __get__(self, query: Any, owner: Any) -> Any: ...


class Relation(Query[T]):
    """The type of a reverse (`BackRef`) or many-to-many collection on an instance.

    Annotate the field as `Relation[list[Target]]`. On an instance, the field
    is a query over the related rows: await `.all()` to load them, or chain
    `where()`, `order_by()` and the other query methods first. See
    [Relationships](../guide/relationships.md).

    Examples:
        ```python {test=run prelude=reference}
        ada = await Author.get(1)
        posts = await ada.posts.all()
        assert [post.title for post in posts] == ["Notes on the Analytical Engine"]
        ```
    """

    # NOTE ON TYPING:
    #
    # Users annotate collection relationships as Relation[list[Model]] to encode
    # cardinality (one-to-many / many-to-many). Since Query.all() is typed as list[T],
    # that would naively become list[list[Model]] in IDEs.
    #
    # We fix hinting by overriding Relation.{all,first} with overloads that interpret
    # Relation[T] as a query whose *rows* are model instances, regardless of whether
    # T is written as Model or list[Model] in the field annotation.
    if TYPE_CHECKING:
        all: ClassVar["_RelationAll"]  # type: ignore[assignment]
        first: ClassVar["_RelationFirst"]  # type: ignore[assignment]
    else:
        @terminal
        async def all(self):  # type: ignore[override]
            """Load every related record.

            Returns:
                (list): The related instances, in the query's order.

            Examples:
                ```python {test=run prelude=reference}
                grace = await Author.get(2)
                posts = await grace.posts.all()
                assert [post.title for post in posts] == ["The Education of a Computer"]
                ```
            """
            return await super().all()

        @terminal
        async def first(self):  # type: ignore[override]
            """Load the first related record, or `None` when there is none.

            Returns:
                (Any): The first related instance in the query's order, or `None`.

            Examples:
                ```python {test=run prelude=reference}
                ada = await Author.get(1)
                post = await ada.posts.order_by(lambda post: post.id).first()
                assert post.title == "Notes on the Analytical Engine"
                ```
            """
            return await super().first()

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type, _handler):
        """Let Pydantic accept any value for a relation field."""
        from pydantic_core import core_schema

        return core_schema.any_schema()
