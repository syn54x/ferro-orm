"""The pieces a query lambda builds: row stand-ins, column references and conditions.

A query lambda receives a [`QueryProxy`][ferro.query.QueryProxy]; reading a
field from it gives a `FieldProxy`, and comparing that builds a `QueryNode`
condition.
"""

import difflib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Generic, NoReturn, TypeAlias, TypeVar, get_origin

from .._bind_payload import canonicalize_wire_scalar
from ..columns import retired_clause

TField = TypeVar("TField")
"""The Python type of the column a `FieldProxy` refers to."""
TModel = TypeVar("TModel")
"""The model class a `QueryProxy` stands in for."""

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


def _json_column_shape(spec: Any) -> str | None:
    """``object`` / ``array`` for a json-family column, else ``None`` (#379)."""
    if _aggregate_source_family(spec) != "json":
        return None
    from .._annotation_utils import _strip_optional_and_annotated

    hint = _strip_optional_and_annotated(spec.python_type)
    if hint is list or get_origin(hint) is list:
        return "array"
    return "object"


class QueryNode:
    """A condition in a query: one comparison, or several combined.

    A comparison in a query lambda returns one, such as
    `post.views > 1000`. Combine conditions with `&` (and) and `|` (or),
    and negate one with `~`, which renders SQL `NOT (...)`. Python's own
    `and`, `or` and `not` raise `TypeError`, since they would keep only one
    side. A negated comparison, like SQL `NOT`, leaves out rows where the
    column is `NULL`; see
    [Negation and NULL values](../guide/queries.md#negation-and-null-values).

    Examples:
        ```python {test=run prelude=reference}
        posts = await Post.where(
            lambda post: (post.views > 1000) | (post.title == "The Education of a Computer")
        ).all()
        assert [post.id for post in posts] == [1, 2]

        quiet = await Post.where(lambda post: ~(post.views > 1000)).all()
        assert [post.id for post in quiet] == [2]
        ```
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
        """Build a condition; query lambdas build these for you.

        Args:
            column: The compared column, on a comparison.
            operator: The comparison (`"=="`, `"<"`, `"IN"`, `"LIKE"`, ...),
                or `"AND"` or `"OR"` on a combination.
            value: The value the column is compared with.
            left: The first condition of a combination.
            right: The second condition of a combination.
            is_compound: Whether the node combines `left` and `right`.
            path: The relation fields from the queried model to the column's
                model. Defaults to `()`, the queried model itself.
            child: The negated condition, on a `~` node.
            exists: The details of an existence test, on a node built by
                `.exists()`.
            owner: The model whose stand-in built the comparison, used to
                refuse a condition that mixes two lambdas' parameters.
        """
        self.column = column
        """The compared column, or `None` on a combination."""
        self.operator = operator
        """The comparison or combining operator."""
        self.value = value
        """The value the column is compared with."""
        self.left = left
        """The first condition of a combination."""
        self.right = right
        """The second condition of a combination."""
        self.is_compound = is_compound
        """Whether the node combines `left` and `right`."""
        self.path = path
        """The relation fields from the queried model to the column's model."""
        self.child = child
        """The negated condition, on a `~` node."""
        self.exists = exists
        """The details of an existence test, on a node built by `.exists()`."""
        self.owner = owner
        """The model whose stand-in built the comparison, when known."""

    def __or__(self, other: "QueryNode") -> "QueryNode":
        """Combine two conditions with `OR`.

        Args:
            other: The other condition.

        Returns:
            A condition met when either one is.
        """
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="OR", right=other, is_compound=True)

    def __and__(self, other: "QueryNode") -> "QueryNode":
        """Combine two conditions with `AND`.

        Args:
            other: The other condition.

        Returns:
            A condition met when both are.
        """
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="AND", right=other, is_compound=True)

    def __invert__(self) -> "QueryNode":
        """Negate the condition with `~`, as SQL `NOT (...)`.

        `~` is the only negation: `~post.id.in_(ids)` is `NOT IN` and
        `~post.title.like(pattern)` is `NOT LIKE`.

        Returns:
            A condition met when this one is not.
        """
        return QueryNode(child=self)

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the condition to the dict form Ferro sends with a query.

        Returns:
            The condition and everything under it, as plain dicts and lists.
        """
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
            serialized = canonicalize_wire_scalar(self.value)
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
        """Refuse `bool()`, so that `and`, `or` and `not` cannot drop a condition.

        Python's `and` and `or` keep one operand by its truth value, so
        `(post.views > 1) and (post.id == 2)` would lose a side. Use `&`, `|`
        and `~` instead.

        Raises:
            TypeError: Always.
        """
        raise TypeError(
            "QueryNode cannot be used in a boolean context; use & / | to "
            "combine predicates and ~ to negate them, not and/or/not "
            "(e.g. (u.age >= 18) & ~(u.active == True))."
        )

    def __repr__(self):
        """Show the condition, for debugging."""
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


@dataclass(frozen=True)
class ExistsTest:
    """The details of one existence test, as `author.posts.exists()` builds them.

    `NOT EXISTS` is `~` over the condition that holds the test, like any
    other negation.
    """

    # ``hops`` holds ``QueryJoinHop`` entries and ``joins`` ``QueryJoin``
    # entries, typed loosely because the wire module imports this one.
    hops: tuple[Any, ...]
    """The joins from the queried row to the related rows.

    One for a reverse relation, two through the join table for a
    many-to-many.
    """
    where: tuple["QueryNode", ...]
    """The condition on the related rows; empty for a bare `exists()`."""
    joins: tuple[Any, ...] = ()
    """The joins the condition needs inside the subquery."""
    owner: type | None = None
    """The model whose stand-in built the test."""


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
    """A reference to one column inside a query lambda.

    Reading a field from the lambda's parameter gives one: `post.views` in
    `lambda post: post.views > 1000`. It builds:

    - conditions, with `==`, `!=`, `<`, `<=`, `>`, `>=`,
      [`in_`][ferro.query.nodes.FieldProxy.in_] (or `<<`) and
      [`like`][ferro.query.nodes.FieldProxy.like];
    - aggregates for a `select()` dict, with
      [`count`][ferro.query.nodes.FieldProxy.count],
      [`sum`][ferro.query.nodes.FieldProxy.sum],
      [`avg`][ferro.query.nodes.FieldProxy.avg],
      [`min`][ferro.query.nodes.FieldProxy.min] and
      [`max`][ferro.query.nodes.FieldProxy.max];
    - new values in an `update()` recipe, with `+`, `-` and
      [`merge`][ferro.query.nodes.FieldProxy.merge].

    The type parameter is the column's Python type, so a type checker can
    tell, for example, that `like` needs a text column.

    Examples:
        ```python {test=run prelude=reference}
        posts = await Post.where(lambda post: post.views >= 952).all()
        assert [post.id for post in posts] == [1, 2]
        ```
    """

    def __init__(
        self,
        column: str,
        path: tuple[str, ...] = (),
        owner: type | None = None,
    ):
        """Refer to a column; query lambdas build these for you.

        Args:
            column: The column's name.
            path: The relation fields from the queried model to the column's
                model. Defaults to `()`, the queried model itself.
            owner: The model the column belongs to. Defaults to `None`, which
                skips the column type checks of the aggregate methods.
        """
        self.column = column
        """The column's name."""
        self.path = path
        """The relation fields from the queried model to the column's model."""
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
        """Count the rows where this column is not `NULL`, as SQL `COUNT(column)`.

        Returns:
            An aggregate to name in a `select()` dict. Its value is an `int`,
                `0` over no rows.

        Examples:
            ```python {test=run prelude=reference}
            row = await Post.select(lambda post: {"posts": post.id.count()}).first()
            assert row.posts == 2
            ```
        """
        return self._aggregate("count")

    def sum(self) -> "AggregateExpr":
        """Add up a numeric column, as SQL `SUM(column)`.

        Returns:
            An aggregate to name in a `select()` dict. Its value has the
                column's type, or is `None` over no rows.

        Raises:
            TypeError: The column is not an `int`, `float` or `Decimal`
                column.

        Examples:
            ```python {test=run prelude=reference}
            row = await Post.select(lambda post: {"total": post.views.sum()}).first()
            assert row.total == 2795
            ```
        """
        return self._aggregate("sum")

    def avg(self) -> "AggregateExpr":
        """Average a numeric column, as SQL `AVG(column)`.

        Returns:
            An aggregate to name in a `select()` dict. Its value is a
                `float`, or a `Decimal` for a `Decimal` column, or `None` over
                no rows.

        Raises:
            TypeError: The column is not an `int`, `float` or `Decimal`
                column.

        Examples:
            ```python {test=run prelude=reference}
            row = await Post.select(lambda post: {"mean": post.views.avg()}).first()
            assert row.mean == 1397.5
            ```
        """
        return self._aggregate("avg")

    def min(self) -> "AggregateExpr":
        """Find a column's smallest value, as SQL `MIN(column)`.

        Returns:
            An aggregate to name in a `select()` dict. Its value has the
                column's type, or is `None` over no rows.

        Raises:
            TypeError: The column is not a number, text, date or time column.

        Examples:
            ```python {test=run prelude=reference}
            row = await Post.select(lambda post: {"low": post.views.min()}).first()
            assert row.low == 952
            ```
        """
        return self._aggregate("min")

    def max(self) -> "AggregateExpr":
        """Find a column's largest value, as SQL `MAX(column)`.

        Returns:
            An aggregate to name in a `select()` dict. Its value has the
                column's type, or is `None` over no rows.

        Raises:
            TypeError: The column is not a number, text, date or time column.

        Examples:
            ```python {test=run prelude=reference}
            row = await Post.select(lambda post: {"high": post.views.max()}).first()
            assert row.high == 1843
            ```
        """
        return self._aggregate("max")

    def __iter__(self) -> "NoReturn":
        """Refuse iteration, so Python's `sum(post.views)` points at `post.views.sum()`.

        Raises:
            TypeError: Always.
        """
        raise TypeError(
            f"{self._dotted()} is a column reference and cannot be iterated; "
            f"did you mean {self._dotted()}.sum()? Aggregates are methods on "
            "the column (.count()/.sum()/.avg()/.min()/.max())."
        )

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> "FieldProxy[Any]":
            """Type a field read through a relation for type checkers only.

            `post.author.name` types each step as `FieldProxy[Any]`, so a
            comparison at the end is still a condition. At runtime the
            relation steps are resolved by the row stand-in.

            Args:
                name: The field read through the relation.
            """
            ...

    def __eq__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: "TField | FieldProxy[TField]"
    ) -> QueryNode:
        """Compare for equality; `== None` renders `IS NULL`."""
        return QueryNode(self.column, "==", other, path=self.path, owner=self._owner)

    def __ne__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: "TField | FieldProxy[TField]"
    ) -> QueryNode:
        """Compare for inequality; `!= None` renders `IS NOT NULL`."""
        return QueryNode(self.column, "!=", other, path=self.path, owner=self._owner)

    def __lt__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Compare with `<`."""
        return QueryNode(self.column, "<", other, path=self.path, owner=self._owner)

    def __le__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Compare with `<=`."""
        return QueryNode(self.column, "<=", other, path=self.path, owner=self._owner)

    def __gt__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Compare with `>`."""
        return QueryNode(self.column, ">", other, path=self.path, owner=self._owner)

    def __ge__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Compare with `>=`."""
        return QueryNode(self.column, ">=", other, path=self.path, owner=self._owner)

    def in_(
        self, other: "list[TField] | tuple[TField, ...] | set[TField]"
    ) -> QueryNode:
        """Match a column against a list of values, as SQL `IN`.

        `post.id << {2, 3}` is the same condition. To test membership in
        related rows, use the relation's `exists()` instead of a subquery.

        Args:
            other: The values to match, as a list, tuple or set.

        Returns:
            A condition met when the column equals one of the values.

        Raises:
            TypeError: `other` is not a list, tuple or set; a query is
                refused with a pointer to `exists()`.

        Examples:
            ```python {test=run prelude=reference}
            posts = await Post.where(lambda post: post.id.in_([2, 3])).all()
            assert [post.id for post in posts] == [2]
            ```
        """
        if not isinstance(other, (list, tuple, set)):
            # Late import: builder imports this module at load time.
            # ProjectedQuery and Relation subclass Query, so one check
            # covers every query shape.
            from .builder import Query

            if isinstance(other, Query):
                # in_(subquery) is declined, not deferred (ADR-0007): the
                # workloads it serves are existence-test workloads, and
                # hand-correlating on id columns leaks the join column into
                # every call site.
                raise TypeError(
                    f"The 'in_' operator expects a list, tuple, or set, got "
                    f"{type(other).__name__}. To filter on membership in "
                    "related rows, use the existence test on the relation — "
                    "where(lambda t: t.<relation>.exists(lambda r: ...)) — "
                    "instead of an in_(subquery) (ADR-0007)."
                )
            raise TypeError(
                f"The 'in_' operator expects a list, tuple, or set, got {type(other).__name__}"
            )
        return QueryNode(
            self.column, "IN", list(other), path=self.path, owner=self._owner
        )

    def like(self: "FieldProxy[str]", pattern: str) -> QueryNode:
        """Match a text column against a pattern, as SQL `LIKE`.

        In the pattern, `%` matches any run of characters and `_` one
        character. A type checker accepts `like` on text columns only.

        Args:
            pattern: The pattern, such as `"%Engine%"`.

        Returns:
            A condition met when the column matches.

        Examples:
            ```python {test=run prelude=reference}
            posts = await Post.where(lambda post: post.title.like("%Engine%")).all()
            assert [post.id for post in posts] == [1]
            ```
        """
        return QueryNode(
            self.column, "LIKE", pattern, path=self.path, owner=self._owner
        )

    def __lshift__(
        self, other: "list[TField] | tuple[TField, ...] | set[TField]"
    ) -> QueryNode:
        """Match a column against a list of values with `<<`, the same as `in_`.

        Args:
            other: The values to match, as a list, tuple or set.

        Returns:
            A condition met when the column equals one of the values.
        """
        return self.in_(other)

    def __add__(self, other: Any) -> "BinaryValueExpr":
        """Add to a numeric column in an `update()` recipe."""
        return _binary_value("+", self, other)

    def __sub__(self, other: Any) -> "BinaryValueExpr":
        """Subtract from a numeric column in an `update()` recipe."""
        return _binary_value("-", self, other)

    def __radd__(self, other: Any) -> "BinaryValueExpr":
        """Add a numeric column to a value in an `update()` recipe."""
        return _binary_value("+", other, self)

    def __rsub__(self, other: Any) -> "BinaryValueExpr":
        """Subtract a numeric column from a value in an `update()` recipe."""
        return _binary_value("-", other, self)

    def merge(self, patch: Any) -> "MergeValueExpr":
        """Merge keys into a JSON object column in an `update()` recipe (Postgres only).

        The update renders Postgres `jsonb ||`: keys in `patch` replace the
        same keys in the column, other keys stay, and a `NULL` column counts
        as `{}`. The merge is shallow, so a nested object in `patch` replaces
        the stored one whole. For example,
        `Post.where(...).update(lambda post: {"meta": post.meta.merge({"pinned": True})})`
        sets one key of a `meta` JSON column.

        Args:
            patch: The keys and values to merge in, as a mapping.

        Returns:
            A new value for the column, to assign in an `update()` recipe.

        Raises:
            TypeError: `patch` is not a mapping, or the column does not hold
                a JSON object (a list column, or any other type).
        """
        if isinstance(patch, (list, tuple)):
            raise TypeError(
                f"{self._dotted()}.merge() is object merge; a list patch is "
                ".concat() (not shipped yet)"
            )
        if not isinstance(patch, Mapping):
            raise TypeError(
                f"{self._dotted()}.merge() takes a mapping patch, "
                f"got {type(patch).__name__}"
            )
        if self._owner is not None:
            spec = getattr(self._owner, "__ferro_columns__", {}).get(self.column)
            if spec is not None:
                shape = _json_column_shape(spec)
                if shape == "array":
                    raise TypeError(
                        f"{self._dotted()}.merge() is not supported: "
                        f"{self._owner.__name__}.{self.column} is list-typed; "
                        ".concat() is not shipped yet"
                    )
                if shape != "object":
                    family = _aggregate_source_family(spec)
                    raise TypeError(
                        f"{self._dotted()}.merge() is not supported: "
                        f"{self._owner.__name__}.{self.column} is "
                        f"{family}-typed, and .merge() takes a json object "
                        "(dict or nested model)."
                    )
        return MergeValueExpr(self, dict(patch))

    def __repr__(self):
        """Show the column, for debugging."""
        return f"FieldProxy(column={self.column!r})"


class AggregateExpr:
    """An aggregate over one column, such as `post.views.sum()`, for a `select()` dict.

    Name each aggregate with a dict key:
    `select(lambda post: {"total": post.views.sum()})`. The column may be
    reached through a relation (`post.author.id.count()`). With only
    aggregates, the query returns one record; with plain fields beside them,
    one record per group of those fields. See
    [Aggregations and grouped queries](../guide/aggregations.md).

    An aggregate cannot be compared or used as a condition: `where()`
    filters rows before they are aggregated, and Ferro has no `having()`
    for filtering groups. Filter the rows with `where()`, or compare the
    aggregated results in Python.

    Examples:
        ```python {test=run prelude=reference}
        row = await Post.select(lambda post: {"total": post.views.sum()}).first()
        assert row.total == 2795
        ```
    """

    __slots__ = ("fn", "column", "path")

    def __init__(self, fn: str, column: str, path: tuple[str, ...]) -> None:
        """Build an aggregate; the column methods such as `sum()` build these for you.

        Args:
            fn: The aggregate: `"count"`, `"sum"`, `"avg"`, `"min"` or `"max"`.
            column: The aggregated column.
            path: The relation fields from the queried model to the column's
                model.
        """
        self.fn = fn
        """The aggregate: `"count"`, `"sum"`, `"avg"`, `"min"` or `"max"`."""
        self.column = column
        """The aggregated column."""
        self.path = path
        """The relation fields from the queried model to the column's model."""

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
            'is a projection source (select(lambda t: {"total": '
            f"{self._dotted()}}})), not a predicate."
        )

    def __repr__(self) -> str:
        return (
            f"AggregateExpr(fn={self.fn!r}, column={self.column!r}, path={self.path!r})"
        )


def _numeric_operand_family(value: Any) -> str | None:
    """Classify a ``+`` / ``-`` operand, or ``None`` when already gated."""
    if isinstance(value, FieldProxy):
        if value._owner is None:
            return None
        spec = getattr(value._owner, "__ferro_columns__", {}).get(value.column)
        if spec is None:
            return None
        return _aggregate_source_family(spec)
    if isinstance(value, BinaryValueExpr):
        return None
    if isinstance(value, NowExpr):
        return "datetime"
    if isinstance(value, ValueExpr):
        return "value-expression"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, Decimal):
        return "decimal"
    if value is None:
        return "null"
    return type(value).__name__


def _require_numeric_operand(value: Any, op: str) -> None:
    """Reject a ``+`` / ``-`` operand that is not a numeric family (#378)."""
    family = _numeric_operand_family(value)
    if family is None or family in _AGG_NUMERIC:
        return
    if isinstance(value, FieldProxy) and value._owner is not None:
        model = value._owner.__name__
        raise TypeError(
            f"{value._dotted()} {op} ... is not supported: "
            f"{model}.{value.column} is {family}-typed, and {op} "
            "takes a numeric column (int, float, or Decimal)."
        )
    if isinstance(value, NowExpr):
        raise TypeError(
            f"now is the database clock, not a numeric value; {op} "
            "takes a numeric column (int, float, or Decimal)."
        )
    if isinstance(value, ValueExpr):
        raise TypeError(
            f"this value expression cannot be used with {op}; "
            ".concat() is not shipped yet"
        )
    raise TypeError(
        f"{op} takes a numeric value (int, float, or Decimal), "
        f"got {family}-typed {type(value).__name__}"
    )


def _binary_value(op: str, left: Any, right: Any) -> "BinaryValueExpr":
    _require_numeric_operand(left, op)
    _require_numeric_operand(right, op)
    return BinaryValueExpr(op, left, right)


class ValueExpr:
    """A new value computed in the database, for an `update()` recipe.

    `post.views + 1`, [`now`][ferro.now] and `.merge()` build one. Assign it
    to a column in the dict an `update()` recipe returns. A value expression
    cannot be compared, selected or sorted by: it is only a new value.

    Examples:
        ```python {test=run prelude=reference}
        await Post.where(lambda post: post.id == 1).update(lambda post: {"views": post.views + 1})
        assert (await Post.get(1)).views == 1844
        ```
    """

    def _reject_clause(self, clause: str) -> NoReturn:
        raise TypeError(
            f"value expressions cannot appear in {clause}(); "
            "comparing a value expression to produce a predicate is #327; "
            "projecting or ordering by one is #309."
        )

    def _reject_comparison(self, symbol: str) -> NoReturn:
        raise TypeError(
            f"comparing a value expression ({symbol}) is not a where() "
            "predicate (#327). Value expressions are SET sources "
            '(update(lambda t: {"col": ...})); projecting or ordering by '
            "one is #309."
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
            "ValueExpr has no truth value; it is a SET source "
            '(update(lambda t: {"col": ...})), not a predicate. '
            "Comparing one is #327; projecting or ordering by one is #309."
        )

    def __add__(self, other: Any) -> "BinaryValueExpr":
        """Add a number to the value."""
        return _binary_value("+", self, other)

    def __sub__(self, other: Any) -> "BinaryValueExpr":
        """Subtract a number from the value."""
        return _binary_value("-", self, other)

    def __radd__(self, other: Any) -> "BinaryValueExpr":
        """Add the value to a number."""
        return _binary_value("+", other, self)

    def __rsub__(self, other: Any) -> "BinaryValueExpr":
        """Subtract the value from a number."""
        return _binary_value("-", other, self)

    def __repr__(self) -> str:
        """Show the expression, for debugging."""
        return "ValueExpr()"


class BinaryValueExpr(ValueExpr):
    """A sum or difference in an `update()` recipe, such as `post.views + 1`."""

    def __init__(self, op: str, left: Any, right: Any) -> None:
        """Build the expression; `+` and `-` on a column build these for you.

        Args:
            op: `"+"` or `"-"`.
            left: The left operand: a column, a number or another expression.
            right: The right operand: a column, a number or another
                expression.
        """
        self.op = op
        """`"+"` or `"-"`."""
        self.left = left
        """The left operand."""
        self.right = right
        """The right operand."""

    def __repr__(self) -> str:
        return f"BinaryValueExpr({self.op!r}, {self.left!r}, {self.right!r})"


class NowExpr(ValueExpr):
    """The type of [`now`][ferro.now], the database's clock in an `update()` recipe."""

    def __call__(self) -> NoReturn:
        raise TypeError("now is a singleton, not a function; assign now, not now()")

    def __repr__(self) -> str:
        return "now"


class MergeValueExpr(ValueExpr):
    """A JSON object merge in an `update()` recipe, built by `.merge()`."""

    def __init__(self, left: FieldProxy, patch: dict[str, Any]) -> None:
        """Build the merge; [`merge`][ferro.query.nodes.FieldProxy.merge] builds these for you.

        Args:
            left: The JSON object column to merge into.
            patch: The keys and values to merge in.
        """
        self.left = left
        """The JSON object column to merge into."""
        self.patch = patch
        """The keys and values to merge in."""

    def __repr__(self) -> str:
        return f"MergeValueExpr({self.left!r}, {self.patch!r})"


now = NowExpr()
"""The database's clock, for stamping a `datetime` column in an `update()` recipe.

Assign `now` itself, not `now()`. The update renders SQL
`CURRENT_TIMESTAMP`, so the time comes from the database server, not from
Python.

Examples:
    ```python {test=run prelude=reference}
    from datetime import datetime
    from typing import Annotated

    from ferro import FerroField, Model, create_tables, now


    class Visit(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        page: str
        seen_at: datetime | None = None


    await create_tables()
    visit = await Visit.create(page="/about")
    await Visit.where(lambda visit: visit.page == "/about").update(lambda visit: {"seen_at": now})
    await visit.refresh()
    assert visit.seen_at is not None
    ```
"""


def validate_query_column(model_cls: type, name: str) -> str:
    """Check that a model has a column a query can name.

    A query can name every declared field and the `<relation>_id` column of
    each foreign key.

    Args:
        model_cls: The model class.
        name: The column name.

    Returns:
        `name`, unchanged.

    Raises:
        AttributeError: The model has no such column. The message suggests
            the closest column or relation name and lists the valid ones.
        TypeError: `model_cls` is not a Ferro model.
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
        f"{model_cls.__name__} has no queryable column {name!r}."
        f"{retired_clause(model_cls, name)}{hint} "
        f"Valid columns: {', '.join(sorted(valid))}.{relation_note}",
        name=name,
        obj=model_cls,
    )


class QueryProxy(Generic[TModel]):
    """The stand-in for one row that a query lambda receives.

    Reading a field gives a `FieldProxy` for that column, so
    `lambda post: post.views > 1000` builds a condition. A misspelled name
    raises `AttributeError` with the closest match, before any SQL runs. A
    foreign key relation gives a stand-in for the related row
    (`post.author.name`), and a reverse or many-to-many relation offers
    `exists()` (`author.posts.exists()`). See
    [Typed query predicates](../concepts/query-typing.md).

    Examples:
        ```python {test=run prelude=reference}
        authors = await Author.where(lambda author: author.posts.exists(lambda post: post.views > 1000)).all()
        assert [author.name for author in authors] == ["Ada Lovelace"]
        ```
    """

    # Every attribute types as ``FieldProxy[Any]``: per-field types for a bare
    # lambda parameter need mapped types, proposed for Python in PEP 827.

    __slots__ = ("_model_cls",)

    def __init__(self, model_cls: type) -> None:
        """Stand in for a row of a model; queries build these for you.

        Args:
            model_cls: The queried model class.
        """
        self._model_cls = model_cls

    def __getattr__(self, name: str) -> "FieldProxy[Any]":
        """Read a field: a column, a foreign key relation or a reverse relation.

        Args:
            name: The field name.

        Returns:
            A `FieldProxy` for a column, a `RelationProxy` for a foreign key
            relation, or a `ReverseRelationProxy` for a reverse or
            many-to-many relation.

        Raises:
            AttributeError: The model has no such field.
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
    """The stand-in for a related row, reached through a foreign key relation.

    `post.author` in a query lambda gives one. Reading a field from it
    reaches into the related model (`post.author.name`) and joins that
    model when the query runs; the path can continue through further
    relations. Comparing the relation itself with `==` or `!=` to a saved
    instance or `None` compares the foreign key column, with no join. Other
    comparisons on the relation itself raise `TypeError` naming a column to
    compare instead.

    Examples:
        ```python {test=run prelude=reference}
        ada = await Author.get(1)
        by_ada = await Post.where(lambda post: post.author == ada).all()
        assert [post.id for post in by_ada] == [1]

        by_name = await Post.where(lambda post: post.author.name == "Grace Hopper").all()
        assert [post.id for post in by_name] == [2]
        ```
    """

    __slots__ = ("_root_model", "_path", "_target")

    def __init__(self, root_model: type, path: tuple[str, ...], target: type) -> None:
        """Stand in for a related row; query lambdas build these for you.

        Args:
            root_model: The queried model class.
            path: The relation fields from the queried model to this one.
            target: The related model class.
        """
        self._root_model = root_model
        self._path = path
        self._target = target

    def __getattr__(self, name: str) -> "RelationProxy | FieldProxy[Any]":
        """Read a field of the related model: a column or a further relation.

        Args:
            name: The field name.

        Returns:
            A `FieldProxy` for a column, or a `RelationProxy` one relation
            further.

        Raises:
            AttributeError: The related model has no such field, or the
                field is a reverse relation, which can only be tested with
                `exists()` from the queried model.
        """
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
        """Match rows whose relation is a given instance, or empty with `None`."""
        return self._instance_comparison(other, "==")

    def __ne__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: object
    ) -> QueryNode:
        """Match rows whose relation is not a given instance, or set with `None`."""
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
        """Refuse `in_` on a relation; compare one of its columns instead.

        Args:
            other: Ignored; the call always raises.

        Raises:
            TypeError: Always.
        """
        return self._reject_operator("in_")

    def like(self, other: object) -> "QueryNode":
        """Refuse `like` on a relation; match one of its columns instead.

        Args:
            other: Ignored; the call always raises.

        Raises:
            TypeError: Always.
        """
        return self._reject_operator("like")

    def __lshift__(self, other: object) -> "QueryNode":
        """Refuse `<<` on a relation; compare one of its columns instead."""
        return self._reject_operator("<<")

    def __repr__(self) -> str:
        """Show the relation path, for debugging."""
        joined = ".".join(self._path)
        return f"RelationProxy(path={joined!r}, target={self._target.__name__!r})"


def _resolve_scoped_predicate(
    inner: "Predicate[Any]", model_cls: type, relation: str
) -> QueryNode:
    """Evaluate an existence test's inner lambda over the related model (#315).

    The inner predicate is ordinary ferro — the same validating
    `QueryProxy` a root ``where()`` receives, constructed for the
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
    """The stand-in for a reverse (`BackRef`) or many-to-many relation in a query lambda.

    `author.posts` gives one. Its one method is
    [`exists`][ferro.query.nodes.ReverseRelationProxy.exists], which tests
    whether related rows exist; reading a column, comparing the relation
    or joining it raises with that spelling in the message, since joining
    a collection would repeat the queried rows. See
    [Existence tests](../guide/queries.md#existence-tests-on-reverse-many-to-many-relations).

    Examples:
        ```python {test=run prelude=reference}
        authors = await Author.where(lambda author: author.posts.exists()).all()
        assert len(authors) == 2
        ```
    """

    __slots__ = ("_root_model", "_name", "_spec")

    def __init__(self, root_model: type, name: str, spec: Any) -> None:
        """Stand in for a reverse relation; query lambdas build these for you.

        Args:
            root_model: The queried model class.
            name: The relation's field name.
            spec: The relation's details, from the model.
        """
        self._root_model = root_model
        self._name = name
        self._spec = spec

    def exists(self, inner: "Predicate[Any] | None" = None) -> QueryNode:
        """Test whether the row has related rows, as SQL `EXISTS (...)`.

        Each queried row comes back at most once, however many related rows
        match, so the condition combines with any other, with sorting and
        with paging. Negate it with `~` for rows that have none.

        Args:
            inner: A lambda over the related model that the related rows
                must meet, such as `lambda post: post.views > 1000`. It can
                use every condition a `where()` lambda can, but only its own
                parameter. Defaults to `None`, any related row.

        Returns:
            A condition met when a related row exists.

        Raises:
            ValueError: The queried model, or a many-to-many relation's
                target, has no primary key.
            TypeError: `inner` is not callable, does not return a condition,
                or uses the outer lambda's parameter.

        Examples:
            ```python {test=run prelude=reference}
            await Author.create(name="Mary Somerville", email="mary@example.com")
            without_posts = await Author.where(lambda author: ~author.posts.exists()).all()
            assert [author.name for author in without_posts] == ["Mary Somerville"]
            ```
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
        if self._spec.is_m2m:
            # Two hops through the join table (#316), same node and render
            # loop as the one-hop form: the join table correlates to the
            # enclosing scope, the target joins on inside the subquery. Both
            # hops carry this relation's name — one relation, one alias
            # family.
            target_pk = getattr(self._spec.target, "__ferro_pk__", None)
            if target_pk is None:
                raise ValueError(
                    f"t.{self._name}.exists() requires "
                    f"{self._spec.target.__name__} to declare a primary-key "
                    "column: the M2M existence test joins the join table to "
                    "the target primary key."
                )
            hops = (
                QueryJoinHop(
                    relation=self._name,
                    from_column=root_pk,
                    to_table=self._spec.join_table,
                    to_column=self._spec.source_col,
                    target=self._spec.target,
                ),
                QueryJoinHop(
                    relation=self._name,
                    from_column=self._spec.target_col,
                    to_table=self._spec.target.__ferro_table__,
                    to_column=target_pk,
                    target=self._spec.target,
                ),
            )
        else:
            hops = (
                QueryJoinHop(
                    relation=self._name,
                    from_column=root_pk,
                    to_table=self._spec.target.__ferro_table__,
                    to_column=self._spec.child_fk_column,
                    target=self._spec.target,
                ),
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
                hops=hops, where=where, joins=joins, owner=self._root_model
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
        """Refuse a column read: a reverse relation can only be tested with `exists()`."""
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
        """Refuse `in_` on a reverse relation; use `exists()`.

        Args:
            other: Ignored; the call always raises.

        Raises:
            TypeError: Always.
        """
        return self._reject_operator("in_")

    def like(self, other: object) -> QueryNode:
        """Refuse `like` on a reverse relation; use `exists()`.

        Args:
            other: Ignored; the call always raises.

        Raises:
            TypeError: Always.
        """
        return self._reject_operator("like")

    def __lshift__(self, other: object) -> QueryNode:
        """Refuse `<<` on a reverse relation; use `exists()`."""
        return self._reject_operator("<<")

    def __repr__(self) -> str:
        """Show the relation, for debugging."""
        return (
            f"ReverseRelationProxy(relation={self._name!r}, "
            f"child={self._spec.target.__name__!r})"
        )


Predicate: TypeAlias = Callable[[QueryProxy[TModel]], QueryNode]
"""The type of a query lambda: it takes a row stand-in and returns a condition.

`Predicate[Post]` is `Callable[[QueryProxy[Post]], QueryNode]`, as in
`lambda post: post.views > 1000`.
"""

RowSelector: TypeAlias = Callable[
    [QueryProxy[TModel]],
    FieldProxy[Any]
    | tuple[FieldProxy[Any], ...]
    | list[FieldProxy[Any]]
    | dict[str, "FieldProxy[Any] | AggregateExpr"],
]
"""The type of a `select()` lambda: it takes a row stand-in and returns the fields to keep.

It returns one field (`lambda post: post.title`), a tuple of fields
(`lambda post: (post.id, post.title)`), or a dict whose string keys name the
output fields (`lambda post: {"author": post.author.name}`). A field may be
reached through relations, and a dict value may be an aggregate
(`{"total": post.views.sum()}`). A type checker refuses a comparison, a
nested shape or a key that is not a string.
"""
