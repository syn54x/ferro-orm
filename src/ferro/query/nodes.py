"""Define query AST nodes and field proxies for fluent filtering"""

import difflib
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Generic, TypeAlias, TypeVar

TField = TypeVar("TField")
TModel = TypeVar("TModel")


class QueryNode:
    """Represent a node in the query expression tree

    Attributes:
        column: Column name for leaf nodes.
        operator: Comparison or logical operator.
        value: Right-hand value for leaf comparisons.
        left: Left child node for compound expressions.
        right: Right child node for compound expressions.
        is_compound: Flag indicating whether the node combines two child nodes.

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
        """
        self.column = column
        self.operator = operator
        self.value = value
        self.left = left
        self.right = right
        self.is_compound = is_compound
        self.path = path

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

    def to_ir_dict(self) -> dict[str, Any]:
        """Serialize the query node tree into a QueryIR payload shape."""
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
            "operator": self.operator,
            "left": self.left.to_ir_dict() if self.left else None,
            "right": self.right.to_ir_dict() if self.right else None,
        }

    def __repr__(self):
        """Return a developer-friendly representation of the node"""
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

    def __init__(self, column: str, path: tuple[str, ...] = ()):
        """Initialize a field proxy for a specific column

        Args:
            column: Database column name to target in expressions.
            path: Relation field names from the root model to this column's
                table; empty (default) means the root model. Plumbed through
                so relation traversal (#270) can populate it — this slice
                never produces a non-empty path.
        """
        self.column = column
        self.path = path

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
        return QueryNode(self.column, "==", other, path=self.path)

    def __ne__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: "TField | FieldProxy[TField]"
    ) -> QueryNode:
        """Build an inequality comparison node"""
        return QueryNode(self.column, "!=", other, path=self.path)

    def __lt__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a less-than comparison node"""
        return QueryNode(self.column, "<", other, path=self.path)

    def __le__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a less-than-or-equal comparison node"""
        return QueryNode(self.column, "<=", other, path=self.path)

    def __gt__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a greater-than comparison node"""
        return QueryNode(self.column, ">", other, path=self.path)

    def __ge__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a greater-than-or-equal comparison node"""
        return QueryNode(self.column, ">=", other, path=self.path)

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
        return QueryNode(self.column, "IN", list(other), path=self.path)

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
        return QueryNode(self.column, "LIKE", pattern, path=self.path)

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
    raise AttributeError(
        f"{model_cls.__name__} has no queryable column {name!r}.{hint} "
        f"Valid columns: {', '.join(sorted(valid))}.{relation_note}"
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
        proxy → ``.ledger_id``); every other name falls through to column
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
        validate_query_column(self._model_cls, name)
        return FieldProxy(name)


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
    :class:`QueryNode`; ``where()`` rejects it. Comparison/misuse sugar on a
    relation (``== instance``, ``== None``) is a later slice (#273).

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
        validate_query_column(self._target, name)
        return FieldProxy(name, path=self._path)

    def __repr__(self) -> str:
        joined = ".".join(self._path)
        return f"RelationProxy(path={joined!r}, target={self._target.__name__!r})"


Predicate: TypeAlias = Callable[[QueryProxy[TModel]], QueryNode]
"""Type alias for lambda predicates accepted by :meth:`Query.where`."""
