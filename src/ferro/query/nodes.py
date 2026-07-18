"""Define query AST nodes and field proxies for fluent filtering"""

import difflib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Generic, NoReturn, TypeAlias, TypeVar

TField = TypeVar("TField")
TModel = TypeVar("TModel")

# Aggregate source families (#294, ADR-0009): which column families each
# aggregate accepts, validated at build time. `None` = any column. Families
# with no portable cross-backend meaning — enum (definition order vs lexical
# text order diverges), uuid (no Postgres min/max), json, bool — are outside
# every non-count set; build time is the only honest place to fail them.
_AGG_NUMERIC = frozenset({"integer", "number", "decimal"})
_AGG_ORDERED = _AGG_NUMERIC | {"string", "datetime", "date", "time"}
_AGGREGATE_FAMILIES: dict[str, frozenset[str] | None] = {
    "count": None,
    "sum": _AGG_NUMERIC,
    "avg": _AGG_NUMERIC,
    "min": _AGG_ORDERED,
    "max": _AGG_ORDERED,
}
_AGGREGATE_FAMILY_HINT = {
    "sum": "a numeric column (int, float, or Decimal)",
    "avg": "a numeric column (int, float, or Decimal)",
    "min": "an orderable column (numeric, text, or date/time)",
    "max": "an orderable column (numeric, text, or date/time)",
}


def _aggregate_source_family(spec: Any) -> str:
    """Classify a column spec into an aggregate source family (#294).

    Enum-typed columns classify as ``enum`` regardless of their storage
    family (a text enum's logical type is ``string``, an IntEnum's is
    ``integer`` — both aggregate-hostile for the same portability reason).
    """
    if spec.enum_values is not None or spec.enum_class is not None:
        return "enum"
    return spec.logical_type


class QueryNode:
    """Represent a node in the query expression tree

    Attributes:
        column: Column name for leaf nodes.
        operator: Comparison or logical operator.
        value: Right-hand value for leaf comparisons.
        left: Left child node for compound expressions.
        right: Right child node for compound expressions.
        is_compound: Flag indicating whether the node combines two child nodes.
        child: Negated child node for NOT nodes (``~``, ADR-0008).
        exists: The existence test's facts for exists nodes (ADR-0007).

    Examples:
        >>> active_filter = FieldProxy("active") == True
        >>> admin_filter = FieldProxy("role") == "admin"
        >>> expr = active_filter & admin_filter
        >>> isinstance(expr, QueryNode)
        True
    """

    def __init__(
        self,
        column: str | None = None,
        operator: str | None = None,
        value: Any = None,
        left: "QueryNode | None" = None,
        right: "QueryNode | None" = None,
        is_compound: bool = False,
        path: tuple[str, ...] = (),
        child: "QueryNode | None" = None,
        exists: "ExistsTest | None" = None,
        owner: type | None = None,
    ):
        """Initialize a query expression node

        Args:
            column: Column name for a leaf comparison node.
            operator: Comparison or logical operator string.
            value: Comparison value for leaf nodes.
            left: Left child node for compound expressions.
            right: Right child node for compound expressions.
            is_compound: Set to True for logical expressions with child nodes.
            path: Relation field names from the root model to ``column``'s
                table; empty (default) means the root model. Plumbed through
                so relation traversal (#270) can populate it — this slice
                never produces a non-empty path.
            child: The negated child for a NOT node (built by ``~``,
                ADR-0008); ``None`` for leaf and compound nodes.
            exists: The correlation hops and inner condition tree of an
                existence test (built by ``t.rel.exists()``, ADR-0007);
                ``None`` for every other node kind.
            owner: The model class whose scope built this leaf (the proxy's
                owner), when known. A compile-side fact, never serialized —
                the cross-scope guard on scoped existence tests (#315) reads
                it to catch a leaf smuggled in from another lambda's
                parameter.
        """
        self.column = column
        self.operator = operator
        self.value = value
        self.left = left
        self.right = right
        self.is_compound = is_compound
        self.path = path
        self.child = child
        self.exists = exists
        self.owner = owner

    def __or__(self, other: "QueryNode") -> "QueryNode":
        """Combine two nodes with logical OR

        Args:
            other: Another query node to combine.

        Returns:
            A compound node representing ``self OR other``.

        Examples:
            >>> role = FieldProxy("role")
            >>> expr = (role == "admin") | (role == "owner")
            >>> expr.is_compound
            True
        """
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="OR", right=other, is_compound=True)

    def __and__(self, other: "QueryNode") -> "QueryNode":
        """Combine two nodes with logical AND

        Args:
            other: Another query node to combine.

        Returns:
            A compound node representing ``self AND other``.

        Examples:
            >>> active = FieldProxy("active") == True
            >>> email = FieldProxy("email").like("%@ferro.dev")
            >>> expr = active & email
            >>> expr.is_compound
            True
        """
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="AND", right=other, is_compound=True)

    def __invert__(self) -> "QueryNode":
        """Negate this predicate with prefix ``~`` (ADR-0008)

        One universal rule: ``~`` negates ANY predicate node — leaf
        comparison, AND/OR compound, or a negation itself (``~~p`` nests) —
        by wrapping it in a NOT node rendered as SQL ``NOT (...)``. There are
        no per-operator negative forms: ``~t.col.in_(ids)`` is NOT IN,
        ``~t.col.like(p)`` is NOT LIKE.

        Returns:
            A NOT node wrapping ``self``.

        Examples:
            >>> node = ~FieldProxy("role").in_(["admin", "owner"])
            >>> node.child.operator
            'IN'
        """
        return QueryNode(child=self)

    def to_ir_dict(self) -> dict[str, Any]:
        """Serialize the query node tree into a QueryIR payload shape."""
        if self.child is not None:
            return {"node_kind": "not", "child": self.child.to_ir_dict()}
        if self.exists is not None:
            serialized_exists: dict[str, Any] = {
                "node_kind": "exists",
                "hops": [hop.to_ir_dict() for hop in self.exists.hops],
                "where": [node.to_ir_dict() for node in self.exists.where],
            }
            # The inner-traversal joins key is absent, not empty, on a
            # traversal-free test (#315) — pinned wire bytes, mirroring the
            # Rust skip_serializing_if.
            if self.exists.joins:
                serialized_exists["joins"] = [
                    join.to_ir_dict() for join in self.exists.joins
                ]
            return serialized_exists
        if not self.is_compound:
            serialized = _serialize_query_value(self.value)
            return {
                "node_kind": "leaf",
                "column": self.column,
                "operator": self.operator,
                "value": {"kind": _query_value_kind(serialized), "value": serialized},
                "path": list(self.path),
            }
        return {
            "node_kind": "compound",
            # Identity checks, never truthiness: ``__bool__`` raises to catch
            # ``and``/``or`` misuse, so every internal test of a QueryNode uses
            # ``is (not) None`` (audited for #273).
            "operator": self.operator,
            "left": self.left.to_ir_dict() if self.left is not None else None,
            "right": self.right.to_ir_dict() if self.right is not None else None,
        }

    def __bool__(self) -> bool:
        """Reject boolean coercion of a query node (#273).

        Python evaluates ``and``/``or`` by calling ``bool()`` on the operands
        (and ``not`` coerces its operand the same way), so ``(u.age >= 18) and
        (u.active == True)`` would silently collapse to one branch instead of
        building a compound predicate. Raising here turns that mistake into a
        pointed error at build time; combine predicates with the bitwise
        ``&`` / ``|`` operators and negate with ``~``.

        Raises:
            TypeError: Always — a ``QueryNode`` has no truth value.
        """
        raise TypeError(
            "QueryNode cannot be used in a boolean context; use & / | to "
            "combine predicates and ~ to negate them, not and/or/not "
            "(e.g. (u.age >= 18) & ~(u.active == True))."
        )

    def __repr__(self):
        """Return a developer-friendly representation of the node"""
        if self.child is not None:
            return f"QueryNode(NOT {self.child!r})"
        if self.exists is not None:
            relation = self.exists.hops[0].relation if self.exists.hops else "?"
            return f"QueryNode(EXISTS {relation!r}, where={self.exists.where!r})"
        if not self.is_compound:
            return f"QueryNode(column={self.column!r}, operator={self.operator!r}, value={self.value!r})"
        return (
            f"QueryNode(left={self.left!r}, op={self.operator!r}, right={self.right!r})"
        )


def _serialize_query_value(value: Any) -> Any:
    """Normalize Python values into JSON-friendly query payloads."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_serialize_query_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_query_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class ExistsTest:
    """The facts of one existence test (CONTEXT.md, ADR-0007).

    ``hops`` is the correlation hop path in the ``joins``-section hop-fact
    shape (``QueryJoinHop`` — typed loosely here because the wire module
    imports this one): one hop for a reverse FK, two for M2M. ``where`` is
    the ordinary inner condition tree over the related model — empty for a
    bare test; nesting and negation come free from :class:`QueryNode`
    recursion. There is no negation flag: NOT EXISTS is ``~`` (a ``not``
    node) over the exists node, like every other predicate (ADR-0008).

    ``joins`` carries the inner tree's forward-traversal hop facts (#315,
    ``QueryJoin`` entries, always ``"inner"`` — ADR-0006 semantics inside the
    subquery), serialized only when non-empty. ``owner`` is the model whose
    proxy built this test — a compile-side scope tag for the cross-scope
    guard, never serialized.
    """

    hops: tuple[Any, ...]
    where: tuple["QueryNode", ...]
    joins: tuple[Any, ...] = ()
    owner: type | None = None


def _query_value_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "unknown"


class FieldProxy(Generic[TField]):
    """Capture field comparisons and build query nodes

    ``FieldProxy`` is generic over the column's Python type so that operator
    overloads carry that type into static analysis. At runtime the type
    parameter is erased and the proxy works identically for any column type.

    Attributes:
        column: Database column name associated with the model field.

    Examples:
        >>> email_filter = FieldProxy("email") == "taylor@example.com"
        >>> isinstance(email_filter, QueryNode)
        True
    """

    def __init__(
        self,
        column: str,
        path: tuple[str, ...] = (),
        owner: type | None = None,
    ):
        """Initialize a field proxy for a specific column

        Args:
            column: Database column name to target in expressions.
            path: Relation field names from the root model to this column's
                table; empty (default) means the root model. Plumbed through
                so relation traversal (#270) can populate it — this slice
                never produces a non-empty path.
            owner: The model class this column belongs to (the root model, or
                a traversal's target model), when known. Query proxies always
                pass it; aggregate methods (#294) read the column spec off it
                to validate source families at build time. A hand-built proxy
                without an owner skips that validation.
        """
        self.column = column
        self.path = path
        # Underscored so the attribute can never statically shadow traversal
        # to a relation FIELD named "owner" (`t.account.owner.email`) through
        # the TYPE_CHECKING `__getattr__` chain.
        self._owner = owner

    def _dotted(self) -> str:
        """The selector expression this proxy came from (error messages)."""
        return "t." + ".".join((*self.path, self.column))

    def _aggregate(self, fn: str) -> "AggregateExpr":
        """Build an aggregate expression over this column (#294, ADR-0009).

        Validates the source family at build time when the owning model is
        known: ``sum``/``avg`` need a numeric column, ``min``/``max`` an
        orderable one (numeric, text, date/time), ``count`` takes any column.
        Families with no portable cross-backend meaning (enum, uuid, json,
        bool) are rejected pointedly — build time is the only honest place
        to fail.
        """
        if self._owner is not None:
            spec = getattr(self._owner, "__ferro_columns__", {}).get(self.column)
            if spec is not None:
                family = _aggregate_source_family(spec)
                allowed = _AGGREGATE_FAMILIES[fn]
                if allowed is not None and family not in allowed:
                    model = self._owner.__name__
                    raise TypeError(
                        f"{self._dotted()}.{fn}() is not supported: "
                        f"{model}.{self.column} is {family}-typed, and {fn}() "
                        f"takes {_AGGREGATE_FAMILY_HINT[fn]}. No portable "
                        "cross-backend meaning exists for this aggregate over "
                        f"a {family} column."
                    )
        return AggregateExpr(fn, self.column, self.path)

    def count(self) -> "AggregateExpr":
        """``COUNT(column)`` — counts rows where this column is non-NULL."""
        return self._aggregate("count")

    def sum(self) -> "AggregateExpr":
        """``SUM(column)`` — numeric columns only; ``None`` over no rows."""
        return self._aggregate("sum")

    def avg(self) -> "AggregateExpr":
        """``AVG(column)`` — numeric columns only; ``None`` over no rows."""
        return self._aggregate("avg")

    def min(self) -> "AggregateExpr":
        """``MIN(column)`` — numeric, text, or date/time columns."""
        return self._aggregate("min")

    def max(self) -> "AggregateExpr":
        """``MAX(column)`` — numeric, text, or date/time columns."""
        return self._aggregate("max")

    def __iter__(self) -> "NoReturn":
        """Reject iteration — the builtin-``sum`` trap (#294).

        ``sum(t.amount)`` calls ``iter(t.amount)`` and would otherwise fail
        with an opaque message (or hang on an infinite proxy). Aggregation is
        a method on the proxy, not a Python builtin over it.
        """
        raise TypeError(
            f"{self._dotted()} is a column reference and cannot be iterated; "
            f"did you mean {self._dotted()}.sum()? Aggregates are methods on "
            "the column (.count()/.sum()/.avg()/.min()/.max())."
        )

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> "FieldProxy[Any]":
            """Statically model relation-traversal chaining (#270).

            Only visible to type checkers: ``t.account.ledger_id`` types each
            hop as ``FieldProxy[Any]`` so a traversal comparison still yields a
            ``QueryNode`` while a *bare* proxy (``lambda t: t.account``) stays a
            non-``QueryNode`` and fails predicate typing. At runtime the actual
            resolution lives on :class:`QueryProxy` / :class:`RelationProxy`
            (relation → deeper proxy, column → this ``FieldProxy``); this
            declaration is never executed.
            """
            ...

    def __eq__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: "TField | FieldProxy[TField]"
    ) -> QueryNode:
        """Build an equality comparison node"""
        return QueryNode(self.column, "==", other, path=self.path, owner=self._owner)

    def __ne__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: "TField | FieldProxy[TField]"
    ) -> QueryNode:
        """Build an inequality comparison node"""
        return QueryNode(self.column, "!=", other, path=self.path, owner=self._owner)

    def __lt__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a less-than comparison node"""
        return QueryNode(self.column, "<", other, path=self.path, owner=self._owner)

    def __le__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a less-than-or-equal comparison node"""
        return QueryNode(self.column, "<=", other, path=self.path, owner=self._owner)

    def __gt__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a greater-than comparison node"""
        return QueryNode(self.column, ">", other, path=self.path, owner=self._owner)

    def __ge__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a greater-than-or-equal comparison node"""
        return QueryNode(self.column, ">=", other, path=self.path, owner=self._owner)

    def in_(
        self, other: "list[TField] | tuple[TField, ...] | set[TField]"
    ) -> QueryNode:
        """Build an ``IN`` comparison node from an iterable

        Args:
            other: Collection of values to match against the field.

        Returns:
            A node using the SQL ``IN`` operator.

        Raises:
            TypeError: If ``other`` is not a list, tuple, or set.

        Examples:
            >>> status_filter = FieldProxy("status").in_(["active", "pending"])
            >>> status_filter.operator
            'IN'
        """
        if not isinstance(other, (list, tuple, set)):
            raise TypeError(
                f"The 'in_' operator expects a list, tuple, or set, got {type(other).__name__}"
            )
        return QueryNode(
            self.column, "IN", list(other), path=self.path, owner=self._owner
        )

    def like(self: "FieldProxy[str]", pattern: str) -> QueryNode:
        """Build a ``LIKE`` comparison node

        The ``self: FieldProxy[str]`` annotation prevents type checkers from
        accepting ``.like(...)`` on non-string columns; at runtime the method
        is available on any ``FieldProxy``.

        Args:
            pattern: SQL LIKE pattern such as ``"%@example.com"``.

        Returns:
            A node using the SQL ``LIKE`` operator.

        Examples:
            >>> email_filter = FieldProxy("email").like("%@example.com")
            >>> email_filter.operator
            'LIKE'
        """
        return QueryNode(
            self.column, "LIKE", pattern, path=self.path, owner=self._owner
        )

    def __lshift__(
        self, other: "list[TField] | tuple[TField, ...] | set[TField]"
    ) -> QueryNode:
        """Use ``<<`` as shorthand syntax for ``IN`` comparisons

        Args:
            other: Collection of values to match against the field.

        Returns:
            A node using the SQL ``IN`` operator.

        Examples:
            >>> role_filter = FieldProxy("role") << {"admin", "owner"}
            >>> role_filter.operator
            'IN'
        """
        return self.in_(other)

    def __repr__(self):
        """Return a developer-friendly representation of the field proxy"""
        return f"FieldProxy(column={self.column!r})"


class AggregateExpr:
    """A build-time aggregate expression over one column (#294, ADR-0009).

    Built by the five aggregate methods on :class:`FieldProxy`
    (``t.amount.sum()``, traversal included: ``t.account.balance.avg()``).
    Carries the aggregate ``fn`` (the closed ``count/sum/avg/min/max`` set),
    the source ``column``, and the source's relation ``path`` — the data the
    ``select()`` resolver turns into a v5 ``expr`` record field.

    Deliberately opaque: an aggregate expression is a projection source, not
    a value — comparing one raises pointedly at build time (post-aggregation
    filtering is ``having()``, #291), and it is not a predicate, a column, or
    an iterable.
    """

    __slots__ = ("fn", "column", "path")

    def __init__(self, fn: str, column: str, path: tuple[str, ...]) -> None:
        self.fn = fn
        self.column = column
        self.path = path

    def _dotted(self) -> str:
        """The selector expression this aggregate came from (errors)."""
        return "t." + ".".join((*self.path, self.column)) + f".{self.fn}()"

    def _reject_comparison(self, symbol: str) -> NoReturn:
        """Reject a comparison on an aggregate (#294 → having(), #291)."""
        raise TypeError(
            f"{self._dotted()} {symbol} ... is not a where() predicate: "
            "WHERE filters rows before aggregation, so an aggregate cannot "
            "appear in it. Post-aggregation filtering is having() (#291); "
            "until it lands, filter rows with where() and compare the "
            "aggregated result in Python."
        )

    def __eq__(self, other: object) -> NoReturn:  # type: ignore[override]
        self._reject_comparison("==")

    def __ne__(self, other: object) -> NoReturn:  # type: ignore[override]
        self._reject_comparison("!=")

    def __lt__(self, other: object) -> NoReturn:
        self._reject_comparison("<")

    def __le__(self, other: object) -> NoReturn:
        self._reject_comparison("<=")

    def __gt__(self, other: object) -> NoReturn:
        self._reject_comparison(">")

    def __ge__(self, other: object) -> NoReturn:
        self._reject_comparison(">=")

    def __bool__(self) -> NoReturn:
        raise TypeError(
            f"{self._dotted()} has no truth value; an aggregate expression "
            "is a projection source (select(lambda t: {\"total\": "
            f"{self._dotted()}}})), not a predicate."
        )

    def __repr__(self) -> str:
        return f"AggregateExpr(fn={self.fn!r}, column={self.column!r}, path={self.path!r})"


def validate_query_column(model_cls: type, name: str) -> str:
    """Validate a queryable column name at build time (FF-F F-2).

    Raises:
        AttributeError: If ``name`` is not a declared field or shadow
            ``{fk}_id`` column of ``model_cls``. The message names the bad
            column, suggests the closest valid one across BOTH columns and
            declared forward-relation names (#270), and lists the valid
            columns and relations.
    """
    valid = getattr(model_cls, "__ferro_query_columns__", None)
    if valid is None:
        raise TypeError(
            f"{model_cls!r} is not a registered Ferro model class; "
            "query predicates require a Ferro Model."
        )
    if name in valid:
        return name
    # A valid relation name never reaches here (proxies resolve relations
    # before column validation), but relation field names still enrich the
    # did-you-mean pool so a typo close to a relation gets the right hint.
    relations = getattr(model_cls, "__ferro_relation_specs__", None) or {}
    suggestions = sorted(set(valid) | set(relations))
    close = difflib.get_close_matches(name, suggestions, n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    relation_note = (
        f" Valid relations: {', '.join(sorted(relations))}." if relations else ""
    )
    # `name`/`obj` carry the failing attribute and its model structurally
    # (PEP 678-era AttributeError metadata), so callers with a sharper story
    # for a specific name — include()'s BackRef/M2M guardrail (#287) — can
    # re-raise pointedly without parsing this message.
    raise AttributeError(
        f"{model_cls.__name__} has no queryable column {name!r}.{hint} "
        f"Valid columns: {', '.join(sorted(valid))}.{relation_note}",
        name=name,
        obj=model_cls,
    )


class QueryProxy(Generic[TModel]):
    """Validating attribute proxy passed to lambda predicates (FF-F F-2).

    A fresh ``QueryProxy`` is constructed for the queried model each time a
    lambda predicate is evaluated. Attribute access validates the name
    against the model's queryable columns (declared fields plus shadow
    ``{fk}_id`` columns) and returns a :class:`FieldProxy` — so
    ``lambda user: user.archived == False`` builds a :class:`QueryNode`,
    while ``lambda user: user.archievd == False`` raises ``AttributeError``
    at build time naming the valid columns.

    Attribute types are ``FieldProxy[Any]``: per-field static types for bare
    lambda parameters require TypeScript-style mapped types, proposed for
    Python in PEP 827 (draft, targeting 3.16) — adopted here when type
    checkers support it.

    Examples:
        >>> rows = await User.where(lambda user: user.archived == False).all()  # noqa: E712
    """

    __slots__ = ("_model_cls",)

    def __init__(self, model_cls: type) -> None:
        self._model_cls = model_cls

    def __getattr__(self, name: str) -> "FieldProxy[Any]":
        """Resolve ``name`` on the root model.

        Relation specs are consulted FIRST (#270): a declared forward-FK field
        name yields a :class:`RelationProxy` for traversal (``t.account`` →
        proxy → ``.ledger_id``). A reverse (BackRef) relation name yields a
        :class:`ReverseRelationProxy` exposing the existence test and nothing
        else (#314, ADR-0007). Every other name falls through to column
        validation and returns a :class:`FieldProxy`.
        """
        relations = getattr(self._model_cls, "__ferro_relation_specs__", None) or {}
        spec = relations.get(name)
        if spec is not None:
            # Statically typed as FieldProxy[Any] (chainable shape) even though a
            # relation resolves to a RelationProxy at runtime — a bare relation
            # proxy is intentionally not a QueryNode, so predicate typing still
            # rejects `lambda t: t.account` (design pin, PRD #267).
            return RelationProxy(  # ty: ignore[invalid-return-type]
                self._model_cls, (name,), spec.target
            )
        reverse = getattr(self._model_cls, "__ferro_reverse_specs__", None) or {}
        rspec = reverse.get(name)
        if rspec is not None:
            return ReverseRelationProxy(  # ty: ignore[invalid-return-type]
                self._model_cls, name, rspec
            )
        validate_query_column(self._model_cls, name)
        return FieldProxy(name, owner=self._model_cls)


class RelationProxy:
    """Traversal proxy for a declared forward-FK relation path (#270).

    Returned by attribute access on a :class:`QueryProxy` (or a deeper
    ``RelationProxy``) when the accessed name is a declared forward relation.
    It carries the *root* model, the relation ``path`` walked so far (a tuple
    of relation field names), and the ``target`` model that path resolves to.

    Attribute access resolves against the CURRENT TARGET model, recursively at
    any depth:

    - a relation name yields a deeper ``RelationProxy`` (``path + (name,)``);
    - a column name yields a :class:`FieldProxy` carrying this proxy's ``path``,
      so ``t.account.ledger_id == lid`` builds a path-qualified comparison node;
    - an unknown name raises ``AttributeError`` naming the hop's model with a
      did-you-mean suggestion (same style as :func:`validate_query_column`).

    A bare ``RelationProxy`` returned from a ``where()`` lambda is not a
    :class:`QueryNode`; ``where()`` rejects it. Equality sugar against a
    persisted target instance or ``None`` desugars **join-free** to a
    shadow-FK comparison (#273): ``t.account == acct`` builds
    ``QueryNode(column="account_id", operator="==", value=<acct.pk>, path=())``
    — the shadow column of the LAST hop, with the proxy path MINUS that hop, so
    a deep proxy (``t.account.owner == o``) compares ``owner_id`` under the
    ``("account",)`` prefix joins only.

    Examples:
        >>> proxy = QueryProxy(Transaction)  # doctest: +SKIP
        >>> node = (proxy.account.ledger_id == 7)  # doctest: +SKIP
        >>> node.path  # doctest: +SKIP
        ('account',)
    """

    __slots__ = ("_root_model", "_path", "_target")

    def __init__(
        self, root_model: type, path: tuple[str, ...], target: type
    ) -> None:
        self._root_model = root_model
        self._path = path
        self._target = target

    def __getattr__(self, name: str) -> "RelationProxy | FieldProxy[Any]":
        """Resolve ``name`` against the current target model (one hop deeper)."""
        relations = getattr(self._target, "__ferro_relation_specs__", None) or {}
        spec = relations.get(name)
        if spec is not None:
            return RelationProxy(self._root_model, self._path + (name,), spec.target)
        reverse = getattr(self._target, "__ferro_reverse_specs__", None) or {}
        if name in reverse:
            # A reverse relation reached through forward traversal
            # (`t.account.transactions`) is recognized but has no supported
            # predicate form yet: an existence test correlates to the ROOT
            # scope only (#314). Fail pointedly rather than fall through to
            # the column error's misleading "no queryable column" message.
            dotted = ".".join(("t", *self._path, name))
            raise AttributeError(
                f"{dotted} names the reverse relation {name!r} on a traversed "
                "scope; existence tests are supported on the query root only "
                f"(t.{name}.exists() on a {self._target.__name__} query). "
                "Reverse relations are tested, not traversed (ADR-0007).",
                name=name,
                obj=self._target,
            )
        validate_query_column(self._target, name)
        return FieldProxy(name, path=self._path, owner=self._target)

    def _relation_name(self) -> str:
        """The last-hop relation field name (the one being compared)."""
        return self._path[-1]

    def _last_hop_shadow_column(self) -> tuple[str, type]:
        """Shadow FK column of the LAST hop and the model declaring it.

        For ``t.account`` this is ``account_id`` on the root table (owner =
        root model); for ``t.account.owner`` it is ``owner_id`` on the hop-1
        (account) table. The owner rides the comparison node as a
        compile-side scope fact (#315 cross-scope guard).
        """
        current = self._root_model
        declaring = current
        spec = None
        for name in self._path:
            specs = getattr(current, "__ferro_relation_specs__", None) or {}
            spec = specs[name]
            declaring = current
            current = spec.target
        assert spec is not None  # a RelationProxy always has ≥ 1 hop
        return spec.shadow_column, declaring

    def _instance_comparison(self, other: object, operator: str) -> QueryNode:
        """Desugar ``== instance`` / ``== None`` to a shadow-FK leaf (#273).

        The node targets the last hop's shadow FK column with the proxy path
        MINUS that hop, so it registers only the PREFIX joins (none for a
        one-hop proxy — genuinely join-free).

        Raises:
            ValueError: If ``other`` is a target-model instance whose primary
                key is unset (unpersisted) — naming the model, save-first.
            TypeError: If ``other`` is neither the target model nor ``None`` —
                the relation-vs-scalar guardrail, suggesting a column compare.
        """
        relation = self._relation_name()
        shadow, shadow_owner = self._last_hop_shadow_column()
        prefix_path = self._path[:-1]
        if other is None:
            return QueryNode(
                column=shadow,
                operator=operator,
                value=None,
                path=prefix_path,
                owner=shadow_owner,
            )
        if isinstance(other, self._target):
            # Reuse the Task 3 PK resolver (loud on zero/multiple PKs); local
            # import avoids a wire <-> nodes import cycle at module load.
            from .wire import _target_pk_column

            pk_column = _target_pk_column(self._target)
            pk_value = getattr(other, pk_column, None)
            if pk_value is None:
                raise ValueError(
                    f"cannot compare relation {relation!r} to an unpersisted "
                    f"{self._target.__name__} instance (primary key not set); "
                    "save it first"
                )
            return QueryNode(
                column=shadow,
                operator=operator,
                value=pk_value,
                path=prefix_path,
                owner=shadow_owner,
            )
        raise TypeError(
            f"cannot compare relation {relation!r} to {other!r}: expected a "
            f"{self._target.__name__} instance or None. To filter by a column, "
            f"compare it directly (e.g. t.{relation}.<column> == ...)."
        )

    def __eq__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: object
    ) -> QueryNode:
        """Desugar ``t.<relation> == instance`` / ``== None`` (join-free, #273)."""
        return self._instance_comparison(other, "==")

    def __ne__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: object
    ) -> QueryNode:
        """Desugar ``t.<relation> != instance`` / ``!= None`` (join-free, #273)."""
        return self._instance_comparison(other, "!=")

    def _reject_operator(self, symbol: str) -> "QueryNode":
        """Reject a non-equality operator on a bare relation (#273)."""
        relation = self._relation_name()
        raise TypeError(
            f"relation {relation!r} supports only == / != against a "
            f"{self._target.__name__} instance or None; to compare a column use "
            f"t.{relation}.<column> (e.g. t.{relation}.<column> {symbol} ...)."
        )

    def __lt__(self, other: object) -> "QueryNode":
        return self._reject_operator("<")

    def __le__(self, other: object) -> "QueryNode":
        return self._reject_operator("<=")

    def __gt__(self, other: object) -> "QueryNode":
        return self._reject_operator(">")

    def __ge__(self, other: object) -> "QueryNode":
        return self._reject_operator(">=")

    def in_(self, other: object) -> "QueryNode":
        return self._reject_operator("in_")

    def like(self, other: object) -> "QueryNode":
        return self._reject_operator("like")

    def __lshift__(self, other: object) -> "QueryNode":
        return self._reject_operator("<<")

    def __repr__(self) -> str:
        joined = ".".join(self._path)
        return f"RelationProxy(path={joined!r}, target={self._target.__name__!r})"


def _resolve_scoped_predicate(
    inner: "Predicate[Any]", model_cls: type, relation: str
) -> QueryNode:
    """Evaluate an existence test's inner lambda over the related model (#315).

    The inner predicate is ordinary ferro — the same validating
    :class:`QueryProxy` a root ``where()`` receives, constructed for the
    related model — so every operator, combinator, traversal, and nested
    existence test works unchanged. The rejection shapes mirror
    ``where()``'s own.

    Raises:
        TypeError: If ``inner`` is not callable, returns a bare relation or
            reverse relation, an aggregate, or any other non-predicate value.
    """
    if not callable(inner):
        raise TypeError(
            f"t.{relation}.exists(...) expected a predicate callable over "
            f"{model_cls.__name__} (e.g. `lambda l: l.amount < 0`), got "
            f"{type(inner).__name__}"
        )
    result = inner(QueryProxy(model_cls))
    if isinstance(result, RelationProxy):
        bare = result._path[-1]
        raise TypeError(
            f"t.{relation}.exists(...) inner predicate returned the bare "
            f"relation {bare!r}; compare a column (e.g. l.{bare}.<column> == "
            "...) or use == None / == an instance."
        )
    if isinstance(result, ReverseRelationProxy):
        bare = result._name
        raise TypeError(
            f"t.{relation}.exists(...) inner predicate returned the bare "
            f"reverse relation {bare!r}; test membership with "
            f"l.{bare}.exists()."
        )
    if isinstance(result, AggregateExpr):
        raise TypeError(
            f"t.{relation}.exists(...) cannot filter on the aggregate "
            f"{result._dotted()}: an existence test answers membership, not "
            "aggregation."
        )
    if not isinstance(result, QueryNode):
        raise TypeError(
            f"t.{relation}.exists(...) inner callable must return a "
            f"predicate (QueryNode), got {type(result).__name__}"
        )
    return result


def _validate_inner_scope(node: QueryNode, model_cls: type, relation: str) -> None:
    """Reject cross-scope references in an inner condition tree (#315).

    The inner lambda may reference only its own parameter's scope. A leaf
    built from any other proxy (the outer lambda's parameter), a
    ``FieldProxy`` as a comparison right-hand side (column-to-column), and a
    nested existence test built from another scope's proxy are all
    build-time errors pointing at the deferred capability (#309) — silent
    misrendering is the failure mode this guard exists to prevent.

    Detection reads compile-side facts the proxies stamp on their nodes: a
    leaf's ``owner`` (the model whose proxy built it) checked against the
    model its ``path`` resolves to FROM THE INNER SCOPE, and an exists
    node's ``owner`` (the scope whose proxy built the test).
    """
    if node.child is not None:
        _validate_inner_scope(node.child, model_cls, relation)
        return
    if node.exists is not None:
        if node.exists.owner is not None and node.exists.owner is not model_cls:
            raise TypeError(
                f"t.{relation}.exists(...) inner predicate contains an "
                "existence test built from another scope's parameter "
                f"(over {node.exists.owner.__name__}, not "
                f"{model_cls.__name__}). Cross-scope references inside an "
                "existence test are not supported yet (#309) — the inner "
                "lambda may reference only its own parameter."
            )
        # Its own inner tree was validated against its own scope when built.
        return
    if node.is_compound:
        if node.left is not None:
            _validate_inner_scope(node.left, model_cls, relation)
        if node.right is not None:
            _validate_inner_scope(node.right, model_cls, relation)
        return
    if isinstance(
        node.value,
        (FieldProxy, AggregateExpr, QueryProxy, RelationProxy, ReverseRelationProxy),
    ):
        raise TypeError(
            f"t.{relation}.exists(...) inner predicate compares "
            f"{node.column!r} against another column reference; "
            "column-to-column comparison is cross-scope correlation, not "
            "supported yet (#309) — compare against a value."
        )
    expected = model_cls
    for hop_name in node.path:
        specs = getattr(expected, "__ferro_relation_specs__", None) or {}
        spec = specs.get(hop_name)
        if spec is None:
            raise TypeError(
                f"t.{relation}.exists(...) inner predicate leaf "
                f"{node.column!r} traverses {hop_name!r}, which is not a "
                f"relation of {expected.__name__} — the leaf was built from "
                "another scope's parameter. Cross-scope references are not "
                "supported yet (#309)."
            )
        expected = spec.target
    if node.owner is not None and node.owner is not expected:
        raise TypeError(
            f"t.{relation}.exists(...) inner predicate leaf {node.column!r} "
            f"belongs to {node.owner.__name__}, not the inner scope "
            f"({expected.__name__}) — it was built from another lambda's "
            "parameter. Cross-scope correlation is not supported yet "
            "(#309); the inner lambda may reference only its own parameter."
        )


def _collect_inner_traversal_paths(
    node: QueryNode, paths: dict[tuple[str, ...], None]
) -> None:
    """Collect an inner tree's forward-traversal paths in first-use order.

    The same walk as the builder's ``_register_join_paths`` but scoped to
    one existence test: full paths only (the Rust render dedups shared
    prefixes), and nested exists nodes are skipped — their traversal facts
    ride their own ``joins`` section.
    """
    if node.child is not None:
        _collect_inner_traversal_paths(node.child, paths)
        return
    if node.exists is not None:
        return
    if node.is_compound:
        if node.left is not None:
            _collect_inner_traversal_paths(node.left, paths)
        if node.right is not None:
            _collect_inner_traversal_paths(node.right, paths)
        return
    if node.path:
        paths.setdefault(tuple(node.path))


class ReverseRelationProxy:
    """Predicate proxy for a reverse (BackRef) relation (#314, ADR-0007).

    Returned by attribute access on a :class:`QueryProxy` when the accessed
    name is a resolved reverse relation. It exposes exactly one verb — the
    existence test :meth:`exists` — because reverse relations are *tested*,
    never *traversed*: column access, comparisons (including ``!= None`` /
    ``== None``), and ``in_`` raise at build time with the supported spelling
    in the message. Negation is uniform ``~`` over the returned node
    (ADR-0008), so NOT EXISTS is ``~t.rel.exists()``.
    """

    __slots__ = ("_root_model", "_name", "_spec")

    def __init__(self, root_model: type, name: str, spec: Any) -> None:
        self._root_model = root_model
        self._name = name
        self._spec = spec

    def exists(self, inner: "Predicate[Any] | None" = None) -> QueryNode:
        """Build the existence test: a correlated EXISTS over the child rows.

        Always a correlated EXISTS at every cardinality (a one-to-one BackRef
        renders identically to a to-many one); the result stays root-shaped,
        so the node composes with any other predicate, ordering, and paging.

        Args:
            inner: Optional scoping predicate — a full ferro predicate over
                the related model (#315): every operator, ``&``/``|``/``~``,
                forward traversal (joins rendered INSIDE the subquery,
                ADR-0006 unchanged), and nested existence tests. It may
                reference only its own parameter's scope; cross-scope
                references raise at build time (#309).

        Returns:
            An exists :class:`QueryNode` carrying the one-hop correlation
            path (child table, child FK column against the root PK) and,
            when scoped, the inner condition tree plus its forward-traversal
            join facts.

        Raises:
            ValueError: If the root model declares no primary-key column —
                the EXISTS correlates child FK to root PK, so a PK-less root
                is a loud error, never a guess.
            TypeError: If ``inner`` is not a predicate callable, does not
                return a predicate, or references a scope other than its own
                parameter (cross-scope, #309).
        """
        # Late import: wire.py imports this module (nodes owns the predicate
        # shape, wire owns the hop-fact shape).
        from .wire import QueryJoin, QueryJoinHop, resolve_join_hops

        root_pk = getattr(self._root_model, "__ferro_pk__", None)
        if root_pk is None:
            raise ValueError(
                f"t.{self._name}.exists() requires "
                f"{self._root_model.__name__} to declare a primary-key "
                "column: the existence test correlates the child's FK "
                "against the root primary key."
            )
        hop = QueryJoinHop(
            relation=self._name,
            from_column=root_pk,
            to_table=self._spec.target.__ferro_table__,
            to_column=self._spec.child_fk_column,
            target=self._spec.target,
        )
        where: tuple[QueryNode, ...] = ()
        joins: tuple[Any, ...] = ()
        if inner is not None:
            node = _resolve_scoped_predicate(inner, self._spec.target, self._name)
            _validate_inner_scope(node, self._spec.target, self._name)
            paths: dict[tuple[str, ...], None] = {}
            _collect_inner_traversal_paths(node, paths)
            joins = tuple(
                QueryJoin(
                    join_type="inner",
                    path=resolve_join_hops(self._spec.target, path),
                )
                for path in paths
            )
            where = (node,)
        return QueryNode(
            exists=ExistsTest(
                hops=(hop,), where=where, joins=joins, owner=self._root_model
            )
        )

    def _reject_operator(self, symbol: str) -> NoReturn:
        raise TypeError(
            f"reverse relation {self._name!r} does not support {symbol}: a "
            "reverse relation appears in a predicate only as an existence "
            f"test — t.{self._name}.exists(), negated with "
            f"~t.{self._name}.exists(). Reverse relations are tested, not "
            "traversed (ADR-0007)."
        )

    def __getattr__(self, name: str) -> NoReturn:
        raise AttributeError(
            f"reverse relation {self._name!r} has no queryable column "
            f"{name!r}: reverse relations are tested, not traversed "
            f"(ADR-0007). Test membership with t.{self._name}.exists(), "
            f"negated with ~t.{self._name}.exists().",
            name=name,
            obj=self._root_model,
        )

    def __eq__(self, other: object) -> QueryNode:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        return self._reject_operator("== None" if other is None else "==")

    def __ne__(self, other: object) -> QueryNode:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        return self._reject_operator("!= None" if other is None else "!=")

    def __lt__(self, other: object) -> QueryNode:
        return self._reject_operator("<")

    def __le__(self, other: object) -> QueryNode:
        return self._reject_operator("<=")

    def __gt__(self, other: object) -> QueryNode:
        return self._reject_operator(">")

    def __ge__(self, other: object) -> QueryNode:
        return self._reject_operator(">=")

    def in_(self, other: object) -> QueryNode:
        return self._reject_operator("in_")

    def like(self, other: object) -> QueryNode:
        return self._reject_operator("like")

    def __lshift__(self, other: object) -> QueryNode:
        return self._reject_operator("<<")

    def __repr__(self) -> str:
        return (
            f"ReverseRelationProxy(relation={self._name!r}, "
            f"child={self._spec.target.__name__!r})"
        )


Predicate: TypeAlias = Callable[[QueryProxy[TModel]], QueryNode]
"""Type alias for lambda predicates accepted by :meth:`Query.where`."""

RowSelector: TypeAlias = Callable[
    [QueryProxy[TModel]],
    FieldProxy[Any]
    | tuple[FieldProxy[Any], ...]
    | list[FieldProxy[Any]]
    | dict[str, "FieldProxy[Any] | AggregateExpr"],
]
"""Type alias for lambda selectors accepted by ``select()`` projections.

A selector names fields — one (``lambda t: t.amount``), several
(``lambda t: (t.id, t.amount)``), or a dict whose string keys name the
output fields (``lambda t: {"account_name": t.account.name}`` — output
aliases, #293). Fields may traverse forward-FK relations at any depth, and
dict values may be aggregate expressions (``{"total": t.amount.sum()}`` —
#294; aggregates are user-named, so the dict form is their only home).
Returning a comparison (a :class:`QueryNode`), nesting shapes, or using a
non-string dict key fails the static gate: a projection selects fields,
a predicate belongs in ``where()``.
"""
