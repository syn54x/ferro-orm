"""Build fluent query objects; ``ferro.query.wire`` compiles them to QueryIR.

This module owns chainer-time state and validation (predicates, joins,
projection, includes). Everything whose output is wire shape — the payload
dataclasses, hop-fact resolution, join serialization, materialization plans,
verb policy, and the versioned envelope — lives behind ``wire.compile_query``.
"""

import copy
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generic,
    NamedTuple,
    Never,
    NoReturn,
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
E = TypeVar("E")

# The concrete container type a projected query delivers (parameterized once
# at import; `into=` record types would parameterize per call). Construction
# always goes through Rows._wrap — never pydantic validation (ADR-0007).
_ROWS_OF_ROW: type[Rows[Row]] = Rows[Row]


def _normalize_order_by_nulls(nulls: str | None) -> str | None:
    """Lower-case and validate ``nulls=``; ``None`` stays omitted on the wire."""
    if nulls is None:
        return None
    normalized = nulls.lower()
    if normalized not in ("first", "last"):
        raise ValueError("nulls must be 'first' or 'last'")
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

    ``selector`` is a lambda receiving a validating :class:`QueryProxy` and
    naming a RELATION path (``lambda t: t.account``, ``lambda t: t.account.owner``)
    — i.e. it must return a :class:`RelationProxy`. Each hop is validated against
    the relevant model at build time (the proxy raises ``AttributeError`` with a
    did-you-mean for a bad hop, same as ``where()``).

    Returns:
        The relation ``path`` tuple the selector names (length ≥ 1).

    Raises:
        TypeError: If ``selector`` is not callable, resolves to a column
            (:class:`FieldProxy`) rather than a relation, or returns any other
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
    the predicate proxies resolve (a :class:`ReverseRelationProxy` selector
    result, #314) and one they don't yet (an M2M name, surfaced as an
    ``AttributeError`` and re-raised by
    :func:`_reject_reverse_relation_include`).
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

    ``selector`` is a lambda receiving a validating :class:`QueryProxy` and
    naming a forward-FK RELATION path (``lambda t: t.account``,
    ``lambda t: t.account.owner``) — the same traversal syntax as ``join()``,
    resolving to a :class:`RelationProxy`. Each hop is validated against the
    relevant model at build time (the proxy raises ``AttributeError`` with a
    did-you-mean for a bad hop, exactly like ``where()``).

    Returns:
        The relation ``path`` tuple the selector names (length ≥ 1).

    Raises:
        TypeError: If ``selector`` is a string or otherwise not callable
            (strings never traverse, #280 — include is lambda-selector only),
            resolves to a column (:class:`FieldProxy`) rather than a relation,
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
    selector is dict-returning (an output alias, CONTEXT.md), the bare leaf
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

    ``selector`` receives a validating :class:`QueryProxy` and returns one
    field (``lambda t: t.amount``), a tuple/list of fields
    (``lambda t: (t.id, t.account.name)``), or a dict whose keys name the
    output fields (``lambda t: {"account_name": t.account.name}`` — output
    aliases, CONTEXT.md). Fields may traverse declared forward-FK relations
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
            (did-you-mean, from :func:`validate_query_column`).
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
    leaf column name (CONTEXT.md: Traversed projection).
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

    Keys are output field names (CONTEXT.md: Output alias) and must be
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


class Query(Generic[T]):
    """Build and execute fluent ORM queries.

    Attributes:
        model_cls: Model class used to hydrate results.
        where_clause: Accumulated filter nodes for the query.
        order_by_clause: Sort definitions sent to the Rust core.
    """

    def __init__(
        self, model_cls: Type[T], using: str | None = None, session: Any | None = None
    ):
        """Initialize a query for a model class.

        Args:
            model_cls: Model class that defines the target table.

        Examples:
            >>> query = Query(User)
            >>> query.model_cls is User
            True
        """
        self.model_cls = model_cls
        self._using = using
        self._session = session
        self.where_clause: list["QueryNode"] = []
        self.order_by_clause: list[OrderByEntry] = []
        self._limit: int | None = None
        self._offset: int | None = None
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
        """Add a filter condition to the query.

        ``predicate`` is a lambda of shape ``Callable[[QueryProxy[T]], QueryNode]``.
        The lambda receives a fresh :class:`QueryProxy` whose attributes
        return :class:`FieldProxy` instances, so
        ``lambda user: user.archived == False`` builds a comparison. Column
        names are validated at build time against the model's declared
        fields (plus shadow ``{fk}_id`` columns): a misspelled column raises
        ``AttributeError`` naming the closest valid match, before any query
        is sent to the database.

        Attribute access on a declared forward-FK field traverses the relation
        (``lambda t: t.account.ledger_id == lid``): each hop resolves against
        the related model, and every distinct traversed path renders one INNER
        join (ADR-0006) when the query runs. Every hop is validated at build
        time with the same did-you-mean naming the hop's model.

        Args:
            predicate: A callable that takes a :class:`QueryProxy` and
                returns a :class:`QueryNode`.

        Returns:
            A new ``Query`` with the clause added; ``self`` is unchanged.

        Raises:
            TypeError: If ``predicate`` is not callable, or if it does not
                return a ``QueryNode``.

        Examples:
            >>> q1 = User.where(lambda user: user.archived == False)  # noqa: E712
            >>> q2 = User.where(lambda user: user.id == 1)
            >>> isinstance(q1, Query) and isinstance(q2, Query)
            True
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
        """Project the query to a column subset, or pass through unchanged.

        Bare ``select()`` keeps meaning "full query": it returns an equivalent
        query of complete model instances. With a lambda selector — the
        documented style — the query becomes a projection: its results are
        :class:`Row` records in the list-like :class:`Rows` container, never
        model instances (the complete-instance invariant, ADR-0007). The
        selector returns one field (``select(lambda t: t.amount)``), a tuple
        (``select(lambda t: (t.id, t.amount))``), or a dict whose keys name
        the output fields (``select(lambda t: {"account_name":
        t.account.name})`` — output aliases). Fields may traverse declared
        forward-FK relations at any depth (``t.account.name`` — traversed
        projection); traversal narrows exactly like a ``where()`` predicate
        on the same path (INNER, one join per path, ADR-0006) and
        ``left_join()`` is the opt-out — kept rows decode the traversed
        fields as ``None``. Unaliased traversed fields take the bare leaf
        column name; two fields resolving to the same output name raise at
        build time. Column-name strings (``select("id", "amount")``) follow
        ``order_by``'s string contract exactly: root columns only, never
        traversal, never mixed with a lambda in one call. All forms validate
        at build time with did-you-mean.

        A projected query composes like any other query: ``where()``
        (relation traversal included), ``order_by()`` (by unselected columns
        too), ``limit()``/``offset()``, ``first()``, ``count()``, and
        ``exists()`` all work; ``update()``/``delete()`` and a second
        ``select()`` raise at build time.

        Args:
            *selectors: Nothing (full query), one lambda selector, or
                column-name strings.

        Returns:
            A new query; ``self`` is unchanged. Projected when a selector is
            given.

        Raises:
            TypeError: If the selector is not callable/strings, selects
                non-fields (a bare relation, a comparison), nests shapes
                (a dict inside a tuple, a tuple inside a dict), uses a
                non-string dict key, or mixes strings with a lambda in one
                call.
            ValueError: If the selection is empty, two fields resolve to the
                same output name, a string is a dotted path (strings never
                traverse), or the query already carries an ``include()``
                (one materialization plan per query — record results are
                flat, permanently).

        Examples:
            >>> rows = await Transaction.select(lambda t: (t.id, t.amount)).all()  # doctest: +SKIP
            >>> rows[0].amount  # doctest: +SKIP
            Decimal('12.50')
            >>> named = await Transaction.select(  # doctest: +SKIP
            ...     lambda t: {"id": t.id, "account_name": t.account.name}
            ... ).all()
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
        """Add an ordering clause and return a new query.

        Accepts a lambda naming the column (``order_by(lambda u: u.created_at,
        "desc")`` — the documented style, matching ``where`` predicates) or a
        column-name string (``order_by("created_at", "desc")``). Both forms are
        validated against the model's queryable columns at build time.

        A lambda selector may traverse a declared forward-FK relation
        (``order_by(lambda t: t.account.name)``) exactly like ``where()``: each
        hop resolves against the related model, and the traversed path renders
        one INNER join (ADR-0006), shared with any ``where()`` traversal of the
        same path in the same query — the same path referenced in both yields
        exactly one join. String selectors do not traverse: ``"account.name"``
        is looked up as a literal (unqualified) column name on the queried
        model.

        Args:
            field: Column selector — lambda receiving a :class:`QueryProxy`,
                or a column-name string.
            direction: ``"asc"`` (default) or ``"desc"``.
            nulls: Optional ``"first"`` or ``"last"`` null placement (keyword-
                only). Omitted when unset.

        Returns:
            A new ``Query`` with the ordering added; ``self`` is unchanged.

        Raises:
            AttributeError: If the column is not a queryable column.
            TypeError: If a lambda selector returns something other than a
                single column reference (a bare relation, e.g.
                ``lambda t: t.account``, is meaningless as a sort key).
            ValueError: If ``direction`` is not ``"asc"`` or ``"desc"``, or
                ``nulls`` is not ``"first"`` or ``"last"``.

        Examples:
            >>> newest = await Post.select().order_by(lambda p: p.created_at, "desc").all()
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
        """Force an INNER join on a relation path and return a new query.

        ``selector`` is a lambda naming a RELATION path
        (``lambda t: t.account``, ``lambda t: t.account.owner``) — the same
        traversal syntax as ``where()``, but resolving to the relation itself,
        not a column on it. A bare ``.join(lambda t: t.account)`` with no
        predicate is a meaningful **existence filter** on a nullable relation:
        it narrows the result to rows where the relation exists (ADR-0006).

        Every edge of the path is marked explicit-INNER; combining it with an
        explicit ``.left_join`` on the same edge is a build-time error (see
        :meth:`left_join`).

        Args:
            selector: A lambda receiving a :class:`QueryProxy` and returning a
                relation path (a ``RelationProxy``).

        Returns:
            A new ``Query`` with the join registered; ``self`` is unchanged.

        Raises:
            TypeError: If ``selector`` is not callable or does not resolve to a
                relation path (a column selector is rejected — join names a
                relation, not a column).
            ValueError: If an edge of the path is already marked explicit-LEFT.

        Examples:
            >>> with_account = QJTransaction.select().join(lambda t: t.account)
        """
        return self._add_explicit_join(selector, "inner")

    def left_join(self, selector: "Callable[[QueryProxy[T]], Any]") -> Self:
        """Mark a relation path LEFT (whole-path) and return a new query.

        ``selector`` names a RELATION path exactly like :meth:`join`. Every edge
        of the path is marked LEFT (the **whole-path rule**, ADR-0006), so a
        left-marked 2-hop path retains rows missing the relation at either hop.
        Relation-less rows are retained (NULL retention), observable both in
        ordered results and in traversal predicates on related columns (e.g.
        ``where(lambda t: t.account.name == None)``).

        Conflict rules: an explicit LEFT beats implicit ``where()``/``order_by()``
        traversal on a shared edge (the path renders LEFT); an explicit ``.join``
        plus an explicit ``.left_join`` on the same edge is a build-time error.

        Args:
            selector: A lambda receiving a :class:`QueryProxy` and returning a
                relation path (a ``RelationProxy``).

        Returns:
            A new ``Query`` with the LEFT join registered; ``self`` is unchanged.

        Raises:
            TypeError: If ``selector`` is not callable or does not resolve to a
                relation path.
            ValueError: If an edge of the path is already marked explicit-INNER.

        Examples:
            >>> keep_orphans = QJNote.select().left_join(lambda n: n.account)
        """
        return self._add_explicit_join(selector, "left")

    def include(self, selector: "Callable[[QueryProxy[T]], Any]") -> Self:
        """Populate a relation path on every result and return a new query.

        ``selector`` is a lambda naming a forward-FK RELATION path
        (``lambda t: t.account``, ``lambda t: t.account.owner``) — the same
        traversal syntax as :meth:`join`. Results are the same ``list[T]``
        the query always returned, in one SQL statement, with each included
        relation **populated**: access is a plain attribute
        (``txn.account.label`` — no await, no query) returning the complete
        related instance, exactly as the field's declared annotation claims
        (ADR-0008). Unpopulated relations keep the awaitable contract
        unchanged; a nullable FK with no target populates as ``None`` with
        the root row retained.

        Include decides attached data only — joins decide membership,
        projection decides shape. Adding ``.include(...)`` to any query
        returns exactly the rows that query returned without it: include
        contributes no join-type opinion on any edge a predicate or explicit
        chainer references, and renders LEFT only on edges nothing else
        touches. ``count()``/``exists()`` are unaffected.

        Cumulative, order-free, and idempotent: chained includes accumulate,
        shared prefixes dedup by path identity, and including a path
        populates every hop along it.

        Args:
            selector: A lambda receiving a :class:`QueryProxy` and returning
                a relation path (a ``RelationProxy``).

        Returns:
            A new ``Query`` with the population registered; ``self`` is
            unchanged.

        Raises:
            TypeError: If ``selector`` is a string or not callable, resolves
                to a column rather than a relation, or names a BackRef/M2M
                relation (reverse population is a separate future mechanism).

        Examples:
            >>> txns = await Transaction.select().include(lambda t: t.account).all()  # doctest: +SKIP
            >>> txns[0].account.label  # doctest: +SKIP
            'checking'
        """
        path = _resolve_include_selector(selector, self.model_cls)
        new = self._clone()
        new._includes.setdefault(path, None)
        return new

    def _add_explicit_join(
        self, selector: "Callable[[QueryProxy[T]], Any]", join_type: str
    ) -> Self:
        """Shared body of :meth:`join`/:meth:`left_join` (#272).

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
        """Limit the number of records returned

        Args:
            value: The maximum number of records to return.

        Returns:
            A new ``Query`` with the clause added; ``self`` is unchanged.

        Examples:
            >>> query = User.select().limit(10)
            >>> query._limit
            10
        """
        new = self._clone()
        new._limit = value
        return new

    def offset(self, value: int) -> Self:
        """Skip a specific number of records

        Args:
            value: The number of records to skip.

        Returns:
            A new ``Query`` with the clause added; ``self`` is unchanged.

        Examples:
            >>> query = User.select().offset(20)
            >>> query._offset
            20
        """
        new = self._clone()
        new._offset = value
        return new

    async def all(self) -> list[T]:
        """Return all model instances that match the current query

        Returns:
            A list of model instances.

        Examples:
            >>> users = await User.where(lambda user: user.active == True).all()  # noqa: E712
            >>> isinstance(users, list)
            True
        """
        compiled = compile_query(self, "fetch")
        route = await self._transaction_or_using()
        return await fetch_filtered(
            self.model_cls,
            compiled.wire_json,
            route,
            hop_classes=compiled.hop_classes,
        )

    async def count(self) -> int:
        """Return the number of records that match the current query

        Returns:
            The count of matching records.

        Examples:
            >>> total = await User.where(lambda user: user.active == True).count()  # noqa: E712
            >>> isinstance(total, int)
            True
        """
        compiled = compile_query(self, "count")
        route = await self._transaction_or_using()
        return await count_filtered(
            model_identity(self.model_cls),
            compiled.wire_json,
            route,
        )

    async def update(
        self,
        recipe: Callable[[QueryProxy[T]], dict[str, Any]] | None = None,
        /,
        **fields: Any,
    ) -> int:
        """Update all records matching the current query.

        Two doors, one SET wire: keyword literals (``update(name="x")``) or a
        recipe callable (``update(lambda user: {"email": "x", "bonus":
        user.score, "n": user.n + 1, "updated_at": now})``). Recipe values
        are a literal, a root column copy, ``+`` / ``-`` arithmetic, or the
        imported ``now`` singleton. ``+`` does not fill empties — ``NULL + 1``
        is NULL (``.merge()`` fills, #379). The doors cannot be mixed.

        Args:
            recipe: Optional positional callable receiving a
                :class:`QueryProxy` and returning a non-empty ``dict`` of
                root-column assignments.
            **fields: Field names and literal values to update.

        Returns:
            The number of records updated.

        Raises:
            TypeError: If both doors are used, a recipe is not callable or
                does not return a dict, or a keyword value is not a literal.
            ValueError: If the recipe is empty, ``limit()`` or ``offset()``
                was set, or the query traverses a relation — multi-table
                mutation has no portable SQL.

        Examples:
            >>> updated = await User.where(lambda user: user.id == 1).update(name="Taylor")
            >>> isinstance(updated, int)
            True
            >>> from ferro import now
            >>> n = await Counter.where(lambda counter: counter.id == cid).update(
            ...     lambda counter: {
            ...         "n": counter.n + 1,
            ...         "total": counter.a + counter.b,
            ...         "updated_at": now,
            ...     }
            ... )
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
        return await update_filtered(
            model_identity(self.model_cls),
            compiled.wire_json,
            route,
        )

    async def first(self) -> T | None:
        """Return the first matching record, or None

        Returns:
            A model instance or None.

        Examples:
            >>> user = await User.select().order_by("id").first()
            >>> user is None or isinstance(user, User)
            True
        """
        results = await self.limit(1).all()
        return results[0] if results else None

    async def delete(self) -> int:
        """Delete all records matching the current query

        Returns:
            The number of records deleted.

        Raises:
            ValueError: If ``limit()`` or ``offset()`` was set on this query, or
                if the query traverses a relation (a ``where()`` predicate on a
                related column, or an explicit ``join()``/``left_join()``) —
                multi-table mutation has no portable SQL. Filter by a column on
                the target model, or resolve the related primary keys first and
                delete by primary-key set.

        Examples:
            >>> deleted = await User.where(lambda user: user.disabled == True).delete()  # noqa: E712
            >>> isinstance(deleted, int)
            True
        """
        compiled = compile_query(self, "delete")
        route = await self._transaction_or_using()
        return await delete_filtered(
            model_identity(self.model_cls),
            compiled.wire_json,
            route,
        )

    async def exists(self) -> bool:
        """Return whether at least one record matches the current query

        Returns:
            True if records exist, otherwise False.

        Examples:
            >>> found = await User.where(lambda user: user.email == "a@b.com").exists()
            >>> isinstance(found, bool)
            True
        """
        return await self.count() > 0

    async def add(self, *instances: Any) -> None:
        """Add links to a many-to-many relationship

        Args:
            *instances: Target model instances that provide an ``id`` attribute.

        Raises:
            RuntimeError: If the query is not bound to a many-to-many context.

        Examples:
            >>> user = await User.create(email="taylor@example.com")
            >>> admin = await Group.create(name="admin")
            >>> staff = await Group.create(name="staff")
            >>> await user.groups.add(admin, staff)
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
        """Remove links from a many-to-many relationship

        Args:
            *instances: Target model instances that provide an ``id`` attribute.

        Raises:
            RuntimeError: If the query is not bound to a many-to-many context.

        Examples:
            >>> user = await User.create(email="taylor@example.com")
            >>> admin = await Group.create(name="admin")
            >>> await user.groups.remove(admin)
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
        """Clear all links in a many-to-many relationship

        Raises:
            RuntimeError: If the query is not bound to a many-to-many context.

        Examples:
            >>> user = await User.create(email="taylor@example.com")
            >>> await user.groups.clear()
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
        """Return a developer-friendly representation of the query"""
        return f"<Query model={self.model_cls.__name__} where={self.where_clause}>"


class ProjectedQuery(Query[T]):
    """A query projected to a column subset: results are records, not models.

    Created by ``select()`` with a selector; carries a ``record``
    materialization plan (ADR-0007), so ``all()`` delivers :class:`Row`
    records in the list-like :class:`Rows` container and ``first()`` a
    ``Row | None`` — never model instances (the complete-instance invariant).
    Projected records bypass the identity map and carry no persistence
    identity.

    Filtering, ordering, and pagination compose exactly like on
    :class:`Query`; ``count()``/``exists()`` are unaffected by the
    projection.
    """

    def __init__(self, source: Query[T], fields: tuple[_ProjectedField, ...]) -> None:
        """Project ``source`` to ``fields`` (selection order preserved).

        Copies the source query's state through ``_clone()`` (fresh mutable
        containers, FF-F F-1); ``source`` is unchanged. Every traversed
        field's relation path registers a join (#293): projection traversal
        narrows exactly like ``where()``/``order_by()`` traversal, sharing
        join identity per relation path (ADR-0006) — ``setdefault`` keeps an
        already-registered path's entry, and an explicit ``left_join()``
        still wins at serialization via the edge marks.
        """
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
        """True when this is an AGGREGATE PROJECTION (CONTEXT.md): at least
        one aggregate field, so every plain field is a group key (#295)."""
        return any(field.agg for field in self._projection)

    def _append_order_by(
        self,
        column: str,
        direction: str,
        path: tuple[str, ...],
        *,
        nulls: str | None = None,
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
        """Add an ordering clause to a projected query (#295's three rules).

        Strings resolve OUTPUT field names first, then root columns — SQL's
        own ORDER BY scoping (``order_by("total")`` sorts by the projected
        aggregate, even if a root column shares the name). The lambda form
        spells SOURCE expressions: a column (traversal included), or an
        aggregate (``order_by(lambda t: t.amount.sum(), "desc")``) which must
        match a projected aggregate field and sorts by its output name.

        On an AGGREGATE projection every sort key must be a group key or an
        aggregate — anything else raises at build time (each group would
        answer with an arbitrary row's value; SQLite would even permit it).
        On a plain projection unselected root columns stay sortable,
        unchanged.

        Args:
            field: Output field name or root column-name string, or a lambda
                naming a source column / aggregate expression.
            direction: ``"asc"`` (default) or ``"desc"``.
            nulls: Optional ``"first"`` or ``"last"`` null placement (keyword-
                only). Omitted when unset.

        Returns:
            A new query with the ordering added; ``self`` is unchanged.

        Raises:
            ValueError: If ``direction`` is invalid, ``nulls`` is invalid, or
                the sort key is not a group key or aggregate on an aggregate
                projection (including an aggregate expression matching no
                projected field).
            AttributeError: If a string names neither an output field nor a
                queryable root column.
            TypeError: If a lambda resolves to a bare relation or any other
                non-sortable value.
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

    def count(self):  # type: ignore[override]
        """Count matching rows — or raise on an aggregate projection (#295).

        On a plain projection ``count()`` stays projection-blind (#279's
        verb contract). On an AGGREGATE projection "count" is ambiguous
        between rows and groups, so it raises synchronously at the call,
        before any coroutine or SQL exists, with both spellings.

        Raises:
            ValueError: On an aggregate projection — count rows with an
                unprojected query, count groups with ``len(await q.all())``.
        """
        if self._has_aggregate():
            raise ValueError(
                "count() on an aggregate projection is ambiguous: count the "
                "matching rows with an unprojected query "
                "(Model.where(...).count()), or count the groups with "
                "len(await q.all())."
            )
        return super().count()

    def exists(self):  # type: ignore[override]
        """Existence check — or raise on an aggregate projection (#295).

        Raises:
            ValueError: On an aggregate projection — a global aggregate
                always yields one record, and "a group exists" is
                ``len(await q.all()) > 0``; test the unprojected query
                instead (``Model.where(...).exists()``).
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

    async def all(self) -> Rows[Row]:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        """Return the projected records for every matching row.

        Returns:
            A :class:`Rows` of :class:`Row` records, fields in selection
            order, wrapped without validation.

        Examples:
            >>> rows = await Transaction.select(lambda t: (t.id, t.amount)).all()  # doctest: +SKIP
            >>> [r.amount for r in rows]  # doctest: +SKIP
            [Decimal('12.50'), Decimal('7.00')]
        """
        compiled = compile_query(self, "fetch")
        route = await self._transaction_or_using()
        records = await fetch_filtered(
            self.model_cls,
            compiled.wire_json,
            route,
            record_cls=Row,
            hop_classes=compiled.hop_classes,
        )
        return _ROWS_OF_ROW._wrap(records)

    async def first(self) -> Row | None:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        """Return the first matching projected record, or None.

        Examples:
            >>> row = await Transaction.select(lambda t: t.amount).order_by("id").first()  # doctest: +SKIP
            >>> row is None or isinstance(row, Row)  # doctest: +SKIP
            True
        """
        results = await self.limit(1).all()
        return results[0] if results else None

    def select(self, *selectors: "RowSelector[T] | str") -> NoReturn:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        """Reject a second projection: it would change the result type
        mid-chain (#280).

        Raises:
            ValueError: Always — build the projection in one ``select()``
                call, or start a new query from the model.
        """
        raise ValueError(
            "select() was already applied to this query; replacing a "
            "projection changes the result type mid-chain. Name every "
            "projected column in one select() call, or start a new query "
            "from the model."
        )

    def include(self: Never, selector: Any) -> NoReturn:  # type: ignore[override]
        """Reject ``include()`` on a projected query (#287, final at #293).

        One materialization plan per query (ADR-0007/ADR-0008), and record
        results are flat, permanently (ADR-0009: flattened-as-final) — a
        record reaches across a relation with traversed projection, never by
        nesting an instance. The ``self: Never`` pin makes this a STATIC
        error at the call site (tests/static_fixtures/bad_includes.py),
        mirroring the mutation pins.

        Raises:
            ValueError: Always — reach across the relation with traversed
                projection, or populate instances through an unprojected
                query.
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
        """Reject ``update()`` on a projected query (#280).

        Raises:
            ValueError: Always — mutate through an unprojected query
                (``Model.where(...).update(...)``).
        """
        raise ValueError(
            "update() is not supported on a projected query: a projection is "
            "a read shape. Build the mutation from the model instead "
            "(e.g. Model.where(...).update(...))."
        )

    def delete(self: Never) -> NoReturn:  # type: ignore[override]
        """Reject ``delete()`` on a projected query (#280).

        Raises:
            ValueError: Always — mutate through an unprojected query
                (``Model.where(...).delete()``).
        """
        raise ValueError(
            "delete() is not supported on a projected query: a projection is "
            "a read shape. Build the mutation from the model instead "
            "(e.g. Model.where(...).delete())."
        )

    def __repr__(self):
        """Return a developer-friendly representation of the projection"""
        return (
            f"<ProjectedQuery model={self.model_cls.__name__} "
            f"fields={[field.name for field in self._projection]} "
            f"where={self.where_clause}>"
        )


class Relation(Query[T]):
    """Represent lazy collection relationship queries with typing support

    Examples:
        >>> class User(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        ...     name: str
        ...     posts: Relation[list["Post"]] = BackRef()

        >>> class Post(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        ...     title: str
        ...     user: Annotated[User, ForeignKey(related_name="posts")]

        >>> user = await User.get(1)
        >>> posts = await user.posts.all()
        >>> isinstance(posts, list)
        True
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

        @overload
        async def all(self: "Relation[list[E]]") -> list[E]: ...

        @overload
        async def all(self: "Relation[E]") -> list[E]: ...

        @overload
        async def first(self: "Relation[list[E]]") -> E | None: ...

        @overload
        async def first(self: "Relation[E]") -> E | None: ...

    async def all(self):  # type: ignore[override]
        return await super().all()

    async def first(self):  # type: ignore[override]
        return await super().first()

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type, _handler):
        """Allow pydantic-core to treat relationships as arbitrary runtime values"""
        from pydantic_core import core_schema

        return core_schema.any_schema()
