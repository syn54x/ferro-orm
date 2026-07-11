"""Build fluent query objects that serialize QueryIR payloads for the Rust core."""

import copy
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generic,
    Never,
    NoReturn,
    Self,
    Type,
    TypeVar,
    cast,
    overload,
)

from .._bind_payload import update_bind_payload
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
    FieldProxy,
    Predicate,
    QueryNode,
    QueryProxy,
    RelationProxy,
    RowSelector,
    _serialize_query_value,
    validate_query_column,
)
from .rows import Row, Rows

if TYPE_CHECKING:
    from .._core import RouteHandle

T = TypeVar("T")
E = TypeVar("E")

# The concrete container type a projected query delivers (parameterized once
# at import; `into=` record types would parameterize per call). Construction
# always goes through Rows._wrap — never pydantic validation (ADR-0007).
_ROWS_OF_ROW: type[Rows[Row]] = Rows[Row]


def _query_ir_payload_to_json(query_payload: dict[str, Any]) -> str:
    """Serialize a QueryIR payload into a versioned IR envelope JSON string.

    Always emits ``ir_version: 4`` (#285 — unconditional bump, exactly like v3
    at #278 and v2 at #269; there is no earlier envelope left anywhere). v4
    gives the ``instances`` materialization kind its field shape (``paths`` of
    hop facts). Python and Rust ship in one wheel, so a single supported
    version is the whole contract (#267 Implementation Decisions).
    """
    import json

    return json.dumps(
        {
            "ir_kind": "query",
            "ir_version": 4,
            "payload": _serialize_query_value(query_payload),
        }
    )


def _model_identity(model_cls: type) -> str:
    """Qualified registry identity of a Ferro model class (FF-E)."""
    return model_cls.__ferro_identity__  # ty: ignore[unresolved-attribute]


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
    if not isinstance(result, QueryNode):
        raise TypeError(
            "where() predicate callable must return QueryNode, "
            f"got {type(result).__name__}"
        )
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
    if node.is_compound:
        if node.left is not None:
            _register_join_paths(node.left, joins)
        if node.right is not None:
            _register_join_paths(node.right, joins)
        return
    if node.path:
        joins.setdefault(tuple(node.path), "inner")


def _where_node_traverses(node: QueryNode) -> bool:
    """True if any leaf under ``node`` carries a non-empty relation path (#273).

    Used by :meth:`Query._mutating_query_def` to reject relation traversal on
    ``update()``/``delete()``. A join-free shadow-FK leaf (``t.account ==
    instance`` desugars to ``path=()``) is NOT traversal and stays allowed.
    """
    if node.is_compound:
        return (node.left is not None and _where_node_traverses(node.left)) or (
            node.right is not None and _where_node_traverses(node.right)
        )
    return bool(node.path)


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
    if not isinstance(result, RelationProxy):
        raise TypeError(
            "join()/left_join() selector must return a relation path "
            f"(e.g. `lambda t: t.account`), got {type(result).__name__}"
        )
    return result._path


def _path_edges(path: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Every edge (prefix of length ≥ 1) of a relation ``path`` (#272)."""
    return [path[:i] for i in range(1, len(path) + 1)]


def _resolve_projection_selector(
    selector: "RowSelector[Any]", model_cls: type
) -> tuple[str, ...]:
    """Resolve a ``select()`` lambda selector into projected column names (#279).

    ``selector`` receives a validating :class:`QueryProxy` and returns one
    column (``lambda t: t.amount``) or a tuple/list of columns
    (``lambda t: (t.id, t.amount)``). Column names validate at build time with
    did-you-mean, exactly like ``where()``/``order_by()`` (the proxy raises
    ``AttributeError`` on a misspelled name before any round-trip).

    Returns:
        The projected column names, in selection order.

    Raises:
        TypeError: If an element is a bare relation (``lambda t: t.account``),
            a comparison (``lambda t: t.id == 1``), or any other non-column
            value — a projection selects columns.
        ValueError: If the selection is empty or names a column twice
            (a duplicate would silently collapse in the record).
        NotImplementedError: If a selected column traverses a relation
            (``lambda t: t.account.name``) — traversed projection is designed
            together with output aliases and aggregations (#282).
    """
    result = selector(QueryProxy(model_cls))
    items = tuple(result) if isinstance(result, (tuple, list)) else (result,)
    if not items:
        raise ValueError(
            "select() projection selected no columns; select at least one "
            "(e.g. `lambda t: (t.id, t.amount)`), or call select() with no "
            "arguments for the full query."
        )
    return _collect_projection_columns(items)


def _resolve_projection_strings(
    names: tuple[str, ...], model_cls: type
) -> tuple[str, ...]:
    """Resolve ``select("id", "amount")`` string selectors (#280).

    Exactly ``order_by``'s string contract: root columns only (declared fields
    plus shadow ``{fk}_id`` columns), validated at build time with
    did-you-mean. Strings never traverse — a dotted path is rejected
    pointedly, permanently (PRD #277); traversed projection is lambda-only
    when it lands (#282).

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
                "rejected permanently; traversed projection will be "
                "lambda-only when it lands (#282). Select root columns "
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


def _collect_projection_columns(items: tuple[Any, ...]) -> tuple[str, ...]:
    """Validate a lambda selector's resolved items into column names (#279)."""
    columns: list[str] = []
    for item in items:
        if isinstance(item, RelationProxy):
            relation = item._path[-1]
            raise TypeError(
                f"select() cannot project the bare relation {relation!r}; "
                f"select a column on it instead (traversed projection like "
                f"t.{relation}.<column> is planned, see below) or select root "
                "columns."
            )
        if isinstance(item, QueryNode):
            raise TypeError(
                "select() selector returned a comparison, not a column "
                "(e.g. `lambda t: t.id == 1`); projections select columns "
                "(`lambda t: t.id`) — put predicates in where()."
            )
        if not isinstance(item, FieldProxy):
            raise TypeError(
                "select() selector must return a column or a tuple of columns "
                f"(e.g. `lambda t: (t.id, t.amount)`), got {type(item).__name__}"
            )
        if item.path:
            dotted = ".".join((*item.path, item.column))
            raise NotImplementedError(
                f"select() cannot project the traversed column {dotted!r} yet: "
                "traversed projection is designed together with output aliases "
                "and aggregations (#282). Project root columns, or fetch the "
                "related model through its own query."
            )
        if item.column in columns:
            raise ValueError(
                f"select() projects column {item.column!r} more than once; "
                "each projected column must be unique."
            )
        columns.append(item.column)
    return tuple(columns)


def _target_pk_column(model_cls: type) -> str:
    """Return the single primary-key column name of a relation target (#270).

    Raises:
        ValueError: If ``model_cls`` has zero or multiple primary-key columns —
            relation traversal joins against exactly one PK column, so an
            ambiguous target is a loud error naming the model, never a guess.
    """
    pks = [
        name
        for name, spec in getattr(model_cls, "__ferro_columns__", {}).items()
        if spec.primary_key
    ]
    if len(pks) != 1:
        raise ValueError(
            f"Relation traversal into {model_cls.__name__!r} requires exactly one "
            f"primary-key column, found {len(pks)}: {sorted(pks)}."
        )
    return pks[0]


def _resolve_join_hops(
    model_cls: type, path: tuple[str, ...]
) -> list[dict[str, str]]:
    """Resolve a relation path into ordered hop facts for the QueryIR ``joins``.

    Each hop's ``relation``/``from_column``/``to_table``/``to_column`` is read
    from the relation-spec chain starting at ``model_cls`` — ``from_column`` is
    the shadow FK column on the source side, ``to_column`` the target's PK.

    Raises:
        ValueError: If a path element is not a declared relation of the current
            model (should not happen — proxies validate at build time), or the
            target has no single primary key (:func:`_target_pk_column`).
    """
    hops: list[dict[str, str]] = []
    current = model_cls
    for relation in path:
        specs = getattr(current, "__ferro_relation_specs__", None) or {}
        spec = specs.get(relation)
        if spec is None:
            raise ValueError(
                f"{current.__name__!r} has no relation {relation!r} for traversal."
            )
        target = spec.target
        hops.append(
            {
                "relation": relation,
                "from_column": spec.shadow_column,
                "to_table": target.__ferro_table__,
                "to_column": _target_pk_column(target),
            }
        )
        current = target
    return hops


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
        self.order_by_clause: list[dict[str, Any]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._m2m_context: dict[str, Any] | None = None
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
        new._m2m_context = (
            dict(self._m2m_context) if self._m2m_context is not None else None
        )
        new._joins = dict(self._joins)
        new._explicit_edges = dict(self._explicit_edges)
        return new

    def _m2m(
        self, join_table: str, source_col: str, target_col: str, source_id: Any
    ) -> Self:
        """Store many-to-many linkage context for relationship operations.

        A new ``Query`` with the m2m context set; ``self`` is unchanged.
        """
        new = self._clone()
        new._m2m_context = {
            "join_table": join_table,
            "source_col": source_col,
            "target_col": target_col,
            "source_id": source_id,
        }
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

    def select(
        self, *selectors: "RowSelector[T] | str"
    ) -> "Self | ProjectedQuery[T]":
        """Project the query to a column subset, or pass through unchanged.

        Bare ``select()`` keeps meaning "full query": it returns an equivalent
        query of complete model instances. With a lambda selector
        (``select(lambda t: (t.id, t.amount))``, or the single-field form
        ``select(lambda t: t.amount)``) — the documented style — the query
        becomes a projection: its results are :class:`Row` records in the
        list-like :class:`Rows` container, never model instances (the
        complete-instance invariant, ADR-0007). Column-name strings
        (``select("id", "amount")``) follow ``order_by``'s string contract
        exactly: root columns only, never traversal, never mixed with a
        lambda in one call. Both forms validate at build time with
        did-you-mean.

        A projected query composes like any other query: ``where()``
        (relation traversal included), ``order_by()`` (by unselected columns
        too), ``limit()``/``offset()``, ``first()``, ``count()``, and
        ``exists()`` all work; ``update()``/``delete()`` and a second
        ``select()`` raise at build time.

        Args:
            *selectors: Nothing (full query), one lambda selector naming root
                columns, or column-name strings.

        Returns:
            A new query; ``self`` is unchanged. Projected when a selector is
            given.

        Raises:
            TypeError: If the selector is not callable/strings, selects
                non-columns (a bare relation, a comparison), or mixes strings
                with a lambda in one call.
            ValueError: If the selection is empty, repeats a column, or a
                string is a dotted path (strings never traverse).
            NotImplementedError: If a lambda selects a traversed column
                (traversed projection lands with #282).

        Examples:
            >>> rows = await Transaction.select(lambda t: (t.id, t.amount)).all()  # doctest: +SKIP
            >>> rows[0].amount  # doctest: +SKIP
            Decimal('12.50')
        """
        if not selectors:
            return self._clone()
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
            return ProjectedQuery(self, columns)
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
        columns = _resolve_projection_selector(
            cast("RowSelector[T]", selector), self.model_cls
        )
        return ProjectedQuery(self, columns)

    def order_by(
        self,
        field: "str | Callable[[QueryProxy[T]], FieldProxy[Any]]",
        direction: str = "asc",
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

        Returns:
            A new ``Query`` with the ordering added; ``self`` is unchanged.

        Raises:
            AttributeError: If the column is not a queryable column.
            TypeError: If a lambda selector returns something other than a
                single column reference (a bare relation, e.g.
                ``lambda t: t.account``, is meaningless as a sort key).
            ValueError: If ``direction`` is not ``"asc"`` or ``"desc"``.

        Examples:
            >>> newest = await Post.select().order_by(lambda p: p.created_at, "desc").all()
        """
        if direction.lower() not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")

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
            {
                "column": col_name,
                "direction": direction.lower(),
                "path": list(path),
            }
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
        edges = _path_edges(path)
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

    def _materialization_ir(self) -> dict[str, Any]:
        """Serialize this query's materialization plan (ADR-0007, v3).

        Every fetching query carries exactly one plan on the wire; a query
        without a projection materializes complete root instances.
        """
        return {"kind": "root_instances"}

    def _serialize_joins(self) -> list[dict[str, Any]]:
        """Serialize collected relation paths into QueryIR ``joins`` entries.

        Emits one entry per registered full path, in insertion order, each
        carrying its ordered hop facts (:func:`_resolve_join_hops`). The Rust
        SELECT walkers assign deterministic ``j{i}_{relation}`` aliases and
        dedup shared prefixes at render time (#270).

        The wire ``join_type`` is resolved from ``_explicit_edges`` at the EDGE
        level so it is already unambiguous (#272): an entry is ``"left"`` iff
        ALL of its edges are explicitly LEFT-marked, else ``"inner"``. Because
        ``.left_join`` is whole-path and the only source of LEFT, a proper
        prefix of a longer path can be LEFT while the deeper hop is INNER — that
        prefix is itself a registered ``_joins`` entry (``left_join`` registers
        its full path) and is emitted as its own ``"left"`` entry; the longer
        entry is emitted ``"inner"``. The Rust edge resolver then renders the
        prefix edges LEFT and the deeper edges INNER (a pure double-check of
        this wire). This also makes "explicit LEFT beats implicit INNER on a
        shared edge" visible in the IR itself.
        """
        entries: list[dict[str, Any]] = []
        for path in self._joins:
            is_left = all(
                self._explicit_edges.get(edge) == "left" for edge in _path_edges(path)
            )
            entries.append(
                {
                    "join_type": "left" if is_left else "inner",
                    "path": _resolve_join_hops(self.model_cls, path),
                }
            )
        return entries

    def _mutating_query_def(self, operation: str) -> dict[str, Any]:
        """Build the QueryIR payload for a mutating operation (update/delete).

        Mutating payloads never carry ``limit``/``offset`` keys: portable SQL
        has no ``UPDATE/DELETE ... LIMIT``, so pagination on a mutation is
        rejected loudly instead of being silently ignored.

        Raises:
            ValueError: If ``limit()``/``offset()`` was set, or if the query
                traverses a relation (a joined/explicit-edge path, or a
                where-clause leaf carrying a non-empty path). Portable SQL has
                no ``UPDATE/DELETE ... JOIN``; a join-free shadow-FK filter
                (``t.account == instance``) stays allowed. Rejected here, before
                any DB round-trip (the Rust guard from #270 stays as boundary
                defense).
        """
        if self._limit is not None or self._offset is not None:
            raise ValueError(
                f"{operation}() does not support limit/offset: portable SQL has "
                f"no {operation.upper()} ... LIMIT. Remove the .limit()/.offset() "
                f"call, or fetch primary keys first and {operation} by "
                "primary-key set."
            )
        if (
            self._joins
            or self._explicit_edges
            or any(_where_node_traverses(node) for node in self.where_clause)
        ):
            raise ValueError(
                f"{operation}() does not support relation traversal: portable SQL "
                f"has no {operation.upper()} ... JOIN. Fetch primary keys via the "
                f"joined query first, then {operation} by primary-key set. "
                "(A join-free relation filter like `t.account == instance` is "
                "allowed.)"
            )
        return {
            "model_name": _model_identity(self.model_cls),
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": [],
            "m2m": None,
            "joins": [],
            # A mutation never materializes a projected result; projection on
            # a mutating query is rejected before this payload is built.
            "materialization": {"kind": "root_instances"},
        }

    def _fetch_query_def(self) -> dict[str, Any]:
        """Build the QueryIR payload for a fetching operation (``all()``).

        Carries the query's own materialization plan — ``root_instances``
        here, a ``record`` plan on a :class:`ProjectedQuery` (ADR-0007).
        """
        return {
            "model_name": _model_identity(self.model_cls),
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": self.order_by_clause,
            "limit": self._limit,
            "offset": self._offset,
            "m2m": self._m2m_context,
            "joins": self._serialize_joins(),
            "materialization": self._materialization_ir(),
        }

    async def all(self) -> list[T]:
        """Return all model instances that match the current query

        Returns:
            A list of model instances.

        Examples:
            >>> users = await User.where(lambda user: user.active == True).all()  # noqa: E712
            >>> isinstance(users, list)
            True
        """
        query_def = self._fetch_query_def()
        route = await self._transaction_or_using()
        return await fetch_filtered(
            self.model_cls,
            _query_ir_payload_to_json(query_def),
            route,
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
        query_def = {
            "model_name": _model_identity(self.model_cls),
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": [],
            "limit": None,
            "offset": None,
            "m2m": self._m2m_context,
            "joins": self._serialize_joins(),
            # count() is unaffected by projection (PRD #277 verb table): it
            # materializes a scalar, so the plan is root_instances even on a
            # projected query.
            "materialization": {"kind": "root_instances"},
        }
        route = await self._transaction_or_using()
        return await count_filtered(
            _model_identity(self.model_cls),
            _query_ir_payload_to_json(query_def),
            route,
        )

    async def update(self, **fields) -> int:
        """Update all records matching the current query

        Args:
            **fields: Field names and values to update.

        Returns:
            The number of records updated.

        Raises:
            ValueError: If ``limit()`` or ``offset()`` was set on this query, or
                if the query traverses a relation (a ``where()`` predicate on a
                related column, or an explicit ``join()``/``left_join()``) —
                multi-table mutation has no portable SQL. Filter by a column on
                the target model, or resolve the related primary keys first and
                update by primary-key set.

        Examples:
            >>> updated = await User.where(lambda user: user.id == 1).update(name="Taylor")
            >>> isinstance(updated, int)
            True
        """
        query_def = self._mutating_query_def("update")
        route = await self._transaction_or_using()
        return await update_filtered(
            _model_identity(self.model_cls),
            _query_ir_payload_to_json(query_def),
            update_bind_payload(fields),
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
        query_def = self._mutating_query_def("delete")
        route = await self._transaction_or_using()
        return await delete_filtered(
            _model_identity(self.model_cls),
            _query_ir_payload_to_json(query_def),
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
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["target_col"],
            self._m2m_context["source_id"],
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
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["target_col"],
            self._m2m_context["source_id"],
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
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["source_id"],
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

    def __init__(self, source: Query[T], columns: tuple[str, ...]) -> None:
        """Project ``source`` to ``columns`` (selection order preserved).

        Copies the source query's state through ``_clone()`` (fresh mutable
        containers, FF-F F-1); ``source`` is unchanged.
        """
        self.__dict__.update(source._clone().__dict__)
        # Immutable, so chained clones share it safely.
        self._projection: tuple[str, ...] = columns

    def _materialization_ir(self) -> dict[str, Any]:
        """Serialize the ``record`` plan: one field per projected column.

        ``name`` is declared separately from ``column`` and each field
        carries a ``path`` so output aliases and traversed projection (#282)
        extend this shape without reshaping it; this epic only ever emits
        ``name == column`` with an empty path.
        """
        return {
            "kind": "record",
            "fields": [
                {"name": column, "column": column, "path": []}
                for column in self._projection
            ],
        }

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
        query_def = self._fetch_query_def()
        route = await self._transaction_or_using()
        records = await fetch_filtered(
            self.model_cls,
            _query_ir_payload_to_json(query_def),
            route,
            record_cls=Row,
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

    # `self: Never` makes mutating a projected query a STATIC error at the
    # call site (pinned by tests/static_fixtures/bad_projections.py) while the
    # runtime bodies raise at build time — synchronously, at the call, before
    # any coroutine or SQL exists (#280). A projection is a read shape;
    # silently ignoring it would make select(...) a no-op on mutations.

    def update(self: Never, **fields: Any) -> NoReturn:  # type: ignore[override]
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
            f"columns={list(self._projection)} where={self.where_clause}>"
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
