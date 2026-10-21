"""The query payload: the one form in which a built query reaches Ferro's core.

Every query the ORM runs is sent as one versioned JSON document.
[`compile_query`][ferro.query.wire.compile_query] turns a built query into a
[`QueryIrPayload`][ferro.query.wire.QueryIrPayload], and
[`to_wire_json`][ferro.query.wire.to_wire_json] serializes it.
"""

# The dataclasses mirror the core's payload structs one for one; golden
# vectors in tests/fixtures/ir_vectors/ pin both sides to the same bytes
# (tests/test_query_wire_vectors.py here, serde round-trips in the crate).
#
# Friend contract: compile_query reads the query's build state (where_clause,
# order_by_clause, _limit, _offset, _after, _before, _joins, _explicit_edges,
# _includes, _m2m_context, _projection) directly. The builder owns chain-time
# validation and state; this module owns everything whose output is wire
# shape.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping

from .. import _bind_payload
from .._bind_payload import (
    canonicalize_wire_scalar,
    update_bind_payload,
)
from ..state import resolve_compile_connection_name
from .nodes import (
    BinaryValueExpr,
    FieldProxy,
    MergeValueExpr,
    NowExpr,
    QueryNode,
    RelationProxy,
    ReverseRelationProxy,
    ValueExpr,
    _AGG_NUMERIC,
    _aggregate_source_family,
    _json_column_shape,
    _query_value_kind,
    now,
    validate_query_column,
)

if TYPE_CHECKING:
    from .builder import Query

# ``update`` and ``delete`` share the mutate arm; they stay distinct values
# so guardrail errors name the caller.
QueryVerb = Literal["fetch", "count", "update", "delete"]
"""What a payload is compiled for: fetching rows, counting, updating or deleting."""

# Always 14 (#395 — unconditional bump). v14 adds an optional ``before``
# position bound on fetch payloads. Python and Rust ship in one wheel, so a
# single supported version is the whole contract.
_IR_VERSION = 14


class _AbsentType:
    """Marks a wire key that is omitted entirely — absent, not ``null``.

    Mutating payloads never carry ``limit``/``offset`` keys (portable SQL has
    no ``UPDATE/DELETE ... LIMIT``); the absent keys are pinned wire bytes,
    distinct from a fetch payload's explicit ``null``.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


_ABSENT = _AbsentType()


def model_identity(model_cls: type) -> str:
    """Return a model class's qualified name in the model registry.

    Args:
        model_cls: The model class.

    Returns:
        The name the registry and the core know the model by.
    """
    return model_cls.__ferro_identity__  # ty: ignore[unresolved-attribute]


def path_edges(path: tuple[str, ...]) -> list[tuple[str, ...]]:
    """List every leading part of a relation path, shortest first.

    Args:
        path: Relation field names, such as `("author", "team")`.

    Returns:
        Each prefix of `path` of length one or more: `[("author",),
            ("author", "team")]`.
    """
    return [path[:i] for i in range(1, len(path) + 1)]


@dataclass(frozen=True)
class QueryJoinHop:
    """One step of a relation path: the join from one table to the next.

    The same shape describes the payload's joins, the paths an `include()`
    fills in and the path of an aggregate's column.
    """

    relation: str
    """The relation field the step follows."""
    from_column: str
    """The column on the source side, the foreign key column."""
    to_table: str
    """The table the step joins."""
    to_column: str
    """The column on the joined table, its primary key."""
    target: type = field(compare=False, repr=False, kw_only=True)
    """The joined model class; kept in Python and never sent."""

    def to_ir_dict(self) -> dict[str, str]:
        """Convert the step to its payload form.

        Returns:
            The step as a dict, without `target`.
        """
        return {
            "relation": self.relation,
            "from_column": self.from_column,
            "to_table": self.to_table,
            "to_column": self.to_column,
        }


@dataclass(frozen=True)
class QueryJoin:
    """One join the query needs: a relation path and its join type."""

    join_type: str
    """`"inner"` or `"left"`."""
    path: tuple[QueryJoinHop, ...]
    """The steps from the queried table to the joined one."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the join to its payload form.

        Returns:
            The join as a dict.
        """
        return {
            "join_type": self.join_type,
            "path": [hop.to_ir_dict() for hop in self.path],
        }


@dataclass(frozen=True)
class OrderByEntry:
    """One sort key of a query, as `order_by()` records it."""

    column: str
    """The sorted column."""
    direction: str
    """`"asc"` or `"desc"`."""
    path: tuple[str, ...]
    """The relation fields from the queried model to the column's model."""
    nulls: str = "last"
    """Where `NULL` values sort: `"first"`, `"last"` or `"native"`."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the sort key to its payload form.

        Returns:
            The sort key as a dict.
        """
        return {
            "column": self.column,
            "direction": self.direction,
            "path": list(self.path),
            "nulls": self.nulls,
        }


@dataclass(frozen=True)
class M2mContext:
    """The join table details of a query over one instance's many-to-many relation."""

    join_table: str
    """The join table."""
    source_col: str
    """The join table column that points at the owning model."""
    target_col: str
    """The join table column that points at the related model."""
    source_id: Any
    """The owning instance's primary key."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the details to their payload form.

        Returns:
            The details as a dict.
        """
        return {
            "join_table": self.join_table,
            "source_col": self.source_col,
            "target_col": self.target_col,
            "source_id": self.source_id,
        }


@dataclass(frozen=True)
class QueryValue:
    """A literal value and its kind, as the payload carries it."""

    kind: str
    """The value's kind: `"null"`, `"bool"`, `"int"`, `"float"`, `"string"`, ..."""
    value: Any
    """The value, in its JSON form."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the value to its payload form.

        Returns:
            The value and its kind, as a dict.
        """
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class LiteralValueExpr:
    """A literal new value in an update."""

    value: QueryValue
    """The value."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the expression to its payload form.

        Returns:
            The expression as a dict.
        """
        return {"kind": "literal", "value": self.value.to_ir_dict()}


@dataclass(frozen=True)
class ColumnRefValueExpr:
    """A new value copied from another column of the same row."""

    column: str
    """The column copied from."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the expression to its payload form.

        Returns:
            The expression as a dict.
        """
        return {"kind": "column", "column": self.column}


@dataclass(frozen=True)
class BinarySetExpr:
    """A new value computed with `+` or `-`."""

    op: Literal["add", "sub"]
    """`"add"` or `"sub"`."""
    left: "SetValueExpr"
    """The left operand."""
    right: "SetValueExpr"
    """The right operand."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the expression to its payload form.

        Returns:
            The expression as a dict.
        """
        return {
            "kind": self.op,
            "left": self.left.to_ir_dict(),
            "right": self.right.to_ir_dict(),
        }


@dataclass(frozen=True)
class NowSetExpr:
    """A new value read from the database's clock."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the expression to its payload form.

        Returns:
            The expression as a dict.
        """
        return {"kind": "now"}


@dataclass(frozen=True)
class MergeSetExpr:
    """A new value merging keys into a JSON object column (Postgres only)."""

    left: "SetValueExpr"
    """The column merged into."""
    right: "SetValueExpr"
    """The keys and values merged in."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the expression to its payload form.

        Returns:
            The expression as a dict.
        """
        return {
            "kind": "merge",
            "left": self.left.to_ir_dict(),
            "right": self.right.to_ir_dict(),
        }


SetValueExpr = (
    LiteralValueExpr | ColumnRefValueExpr | BinarySetExpr | NowSetExpr | MergeSetExpr
)
"""Any new value an update can assign to a column."""


@dataclass(frozen=True)
class SetAssignment:
    """One column an update assigns, and its new value."""

    column: str
    """The assigned column."""
    value: SetValueExpr
    """The new value."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the assignment to its payload form.

        Returns:
            The assignment as a dict.
        """
        return {"column": self.column, "value": self.value.to_ir_dict()}


@dataclass(frozen=True)
class RecordExpr:
    """The aggregate a projected field reads, such as `SUM(views)`.

    It carries its own relation path, apart from the query's joins.
    """

    fn: str
    """The aggregate: `"count"`, `"sum"`, `"avg"`, `"min"` or `"max"`."""
    column: str
    """The aggregated column."""
    path: tuple[QueryJoinHop, ...]
    """The steps from the queried table to the column's table."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the aggregate to its payload form.

        Returns:
            The aggregate as a dict.
        """
        return {
            "fn": self.fn,
            "column": self.column,
            "path": [hop.to_ir_dict() for hop in self.path],
        }


@dataclass(frozen=True)
class RecordField:
    """One field of a projected record: its output name and what it reads.

    A field reads either a column, through `path`, or an aggregate, through
    `expr`; never both and never neither.
    """

    name: str
    """The field's name on the record."""
    column: str | None
    """The column read, or `None` for an aggregate."""
    path: tuple[str, ...]
    """The relation fields from the queried model to the column's model."""
    expr: RecordExpr | None
    """The aggregate read, or `None` for a column."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the field to its payload form.

        Returns:
            The field as a dict, with `column` or `expr` as it reads.
        """
        if self.expr is not None:
            return {
                "name": self.name,
                "path": list(self.path),
                "expr": self.expr.to_ir_dict(),
            }
        return {
            "name": self.name,
            "column": self.column,
            "path": list(self.path),
        }


@dataclass(frozen=True)
class RootInstances:
    """Results as whole instances of the queried model, the default."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the plan to its payload form.

        Returns:
            The plan as a dict.
        """
        return {"kind": "root_instances"}


@dataclass(frozen=True)
class Record:
    """Results as `Row` records holding the selected fields."""

    fields: tuple[RecordField, ...]
    """The record's fields, in selection order."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the plan to its payload form.

        Returns:
            The plan as a dict.
        """
        return {
            "kind": "record",
            "fields": [field.to_ir_dict() for field in self.fields],
        }


@dataclass(frozen=True)
class Instances:
    """Results as instances with the `include()` relations filled in.

    The paths are kept apart from the query's joins, so an `include()` never
    changes which rows come back or how a shared join is made.
    """

    paths: tuple[tuple[QueryJoinHop, ...], ...]
    """One path of steps per included relation."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the plan to its payload form.

        Returns:
            The plan as a dict.
        """
        return {
            "kind": "instances",
            "paths": [[hop.to_ir_dict() for hop in path] for path in self.paths],
        }


Materialization = RootInstances | Record | Instances
"""What a query's results are built as: instances, records or filled-in instances."""


@dataclass(frozen=True)
class QueryIrPayload:
    """Everything one query sends to Ferro's core, field for field.

    An update or delete leaves out `limit` and `offset` entirely, and any
    query leaves out `after` and `before` when they are not set.
    """

    model_name: str
    """The queried model's registry name."""
    where: tuple[dict[str, Any], ...]
    """The conditions, each in the dict form `QueryNode.to_ir_dict()` returns."""
    set: tuple[SetAssignment, ...]
    """The columns an update assigns; empty for other queries."""
    order_by: tuple[OrderByEntry, ...]
    """The sort keys."""
    limit: int | None | _AbsentType
    """The row limit, `None`, or absent on an update or delete."""
    offset: int | None | _AbsentType
    """The rows skipped, `None`, or absent on an update or delete."""
    after: tuple[QueryValue, ...] | None
    """The position the results start after, one value per sort key."""
    before: tuple[QueryValue, ...] | None
    """The position the results end before, one value per sort key."""
    m2m: M2mContext | None
    """The join table details, on a query over a many-to-many relation."""
    joins: tuple[QueryJoin, ...]
    """The joins the query needs."""
    materialization: Materialization
    """What the results are built as."""

    def to_ir_dict(self) -> dict[str, Any]:
        """Convert the payload to its dict form.

        Returns:
            The payload as plain dicts and lists, without the absent keys.
        """
        payload: dict[str, Any] = {
            "model_name": self.model_name,
            "where": list(self.where),
            "set": [assignment.to_ir_dict() for assignment in self.set],
            "order_by": [entry.to_ir_dict() for entry in self.order_by],
        }
        if not isinstance(self.limit, _AbsentType):
            payload["limit"] = self.limit
        if not isinstance(self.offset, _AbsentType):
            payload["offset"] = self.offset
        if self.after:
            payload["after"] = [value.to_ir_dict() for value in self.after]
        if self.before:
            payload["before"] = [value.to_ir_dict() for value in self.before]
        payload["m2m"] = self.m2m.to_ir_dict() if self.m2m is not None else None
        payload["joins"] = [join.to_ir_dict() for join in self.joins]
        payload["materialization"] = self.materialization.to_ir_dict()
        return payload


def _target_pk_column(model_cls: type) -> str:
    """Return the single primary-key column name of a relation target (#270).

    Reads the cached ``__ferro_pk__`` fact (multi-PK models cannot exist past
    class definition), so only the PK-less case remains to guard.

    Raises:
        ValueError: If ``model_cls`` has no primary-key column — relation
            traversal joins against the target's PK, so a PK-less target is a
            loud error naming the model, never a guess.
    """
    pk = getattr(model_cls, "__ferro_pk__", None)
    if pk is None:
        raise ValueError(
            f"Relation traversal into {model_cls.__name__!r} requires a "
            "primary-key column, and it declares none."
        )
    return pk


def resolve_join_hops(
    model_cls: type, path: tuple[str, ...]
) -> tuple[QueryJoinHop, ...]:
    """Turn a relation path into the joins that follow it.

    Args:
        model_cls: The model the path starts from.
        path: Relation field names, such as `("author",)`.

    Returns:
        One step per relation: from the foreign key column on one side to
            the primary key of the next table.

    Raises:
        ValueError: A name in the path is not a relation of the model it is
            read from, or a target model has no primary key.
    """
    hops: list[QueryJoinHop] = []
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
            QueryJoinHop(
                relation=relation,
                from_column=spec.shadow_column,
                to_table=target.__ferro_table__,
                to_column=_target_pk_column(target),
                target=target,
            )
        )
        current = target
    return tuple(hops)


def _where_node_traverses(node: QueryNode) -> bool:
    """True if any leaf under ``node`` carries a non-empty relation path (#273).

    Used by the mutate guardrails to reject relation traversal on
    ``update()``/``delete()``. A join-free shadow-FK leaf (``t.account ==
    instance`` desugars to ``path=()``) is NOT traversal and stays allowed.
    A negation traverses iff its child does (``~`` never adds a path). An
    existence test always counts as traversal: it correlates a subquery to
    another table, and mutations stay single-table write shapes (#314).
    """
    if node.child is not None:
        return _where_node_traverses(node.child)
    if node.exists is not None:
        return True
    if node.is_compound:
        return (node.left is not None and _where_node_traverses(node.left)) or (
            node.right is not None and _where_node_traverses(node.right)
        )
    return bool(node.path)


def _serialize_joins(query: "Query[Any]") -> tuple[QueryJoin, ...]:
    """Serialize the query's registered relation paths into ``joins`` entries.

    Emits one entry per registered full path, in insertion order, each
    carrying its ordered hop facts (`resolve_join_hops`). The Rust
    SELECT walkers assign deterministic ``j{i}_{relation}`` aliases and
    dedup shared prefixes at render time (#270).

    The wire ``join_type`` is resolved from the explicit edge marks at the
    EDGE level so it is already unambiguous (#272): an entry is ``"left"``
    iff ALL of its edges are explicitly LEFT-marked, else ``"inner"``.
    Because ``.left_join`` is whole-path and the only source of LEFT, a
    proper prefix of a longer path can be LEFT while the deeper hop is INNER
    — that prefix is itself a registered path (``left_join`` registers its
    full path) and is emitted as its own ``"left"`` entry; the longer entry
    is emitted ``"inner"``. The Rust edge resolver then renders the prefix
    edges LEFT and the deeper edges INNER (a pure double-check of this
    wire). This also makes "explicit LEFT beats implicit INNER on a shared
    edge" visible in the IR itself.
    """
    entries: list[QueryJoin] = []
    for path in query._joins:
        is_left = all(
            query._explicit_edges.get(edge) == "left" for edge in path_edges(path)
        )
        entries.append(
            QueryJoin(
                join_type="left" if is_left else "inner",
                path=resolve_join_hops(query.model_cls, path),
            )
        )
    return tuple(entries)


def _record_field(model_cls: type, field: Any) -> RecordField:
    """Convert one builder ``_ProjectedField`` into its wire field (v5).

    ``name`` is declared separately from ``column`` (output aliases) and each
    plain field carries its relation ``path`` (traversed projection, #293).
    An aggregate field carries an ``expr`` instead (#294): ``fn`` as data and
    the source's traversal as hop facts on ``expr.path`` — self-contained on
    the wire — with the field-level ``path`` empty.
    """
    if field.agg is not None:
        return RecordField(
            name=field.name,
            column=None,
            path=(),
            expr=RecordExpr(
                fn=field.agg,
                column=field.column,
                path=resolve_join_hops(model_cls, field.path),
            ),
        )
    return RecordField(
        name=field.name, column=field.column, path=tuple(field.path), expr=None
    )


def _materialization(query: "Query[Any]") -> Materialization:
    """Compile the query's materialization plan (ADR-0007).

    Every fetching query carries exactly one plan on the wire: a ``record``
    plan when projected, the ``instances`` plan when it includes relation
    paths (#286 — the same hop shape as ``joins`` but deliberately separate,
    ADR-0008: the fetch walker unions include-only edges in as LEFT, so
    include can never change membership or a shared edge's join type), and
    complete root instances otherwise.
    """
    projection = getattr(query, "_projection", None)
    if projection is not None:
        return Record(
            fields=tuple(_record_field(query.model_cls, field) for field in projection)
        )
    if query._includes:
        return Instances(
            paths=tuple(
                resolve_join_hops(query.model_cls, path) for path in query._includes
            )
        )
    return RootInstances()


_RECIPE_FORM = 'update(lambda t: {"col": ...})'


def _reject_kwargs_value_expr(value: Any) -> None:
    """Kwargs stay literals-only; expressions belong on the recipe door."""
    if isinstance(
        value,
        (FieldProxy, ValueExpr, RelationProxy, ReverseRelationProxy),
    ):
        raise TypeError(
            "update() keyword values must be literals; use the recipe form "
            f"{_RECIPE_FORM} to assign a column or value expression"
        )


def _guarded_literal_assignment(
    model_cls: type, column: str, value: Any
) -> SetAssignment:
    """A literal SET of a root column, refused first if it writes a retired label.

    Raises:
        ValidationError: ``retired_enum_label`` (ADR-0053), before compiling.
    """
    from ..metaclass import refuse_retired_label_write

    refuse_retired_label_write(model_cls, column, value)
    return _literal_assignment(column, value)


def _literal_assignment(column: str, value: Any) -> SetAssignment:
    """Canonicalize one literal (including bytes) into a SET assignment."""
    payload = update_bind_payload({column: value})
    canon = payload[column]
    if isinstance(canon, bytes):
        query_value = QueryValue(kind="bytes", value=list(canon))
    else:
        query_value = QueryValue(kind=_query_value_kind(canon), value=canon)
    return SetAssignment(column=column, value=LiteralValueExpr(value=query_value))


def _literal_set(
    model_cls: type, assignments: Mapping[str, Any] | None
) -> tuple[SetAssignment, ...]:
    """Compile kwargs literals into ordered, typed SET value-expression nodes.

    Each keyword names a queryable column of ``model_cls``, as a recipe's keys
    do: a misspelled or retired one (ADR-0053) fails here, naming it, before
    any statement reaches the database, and so does a retired enum label.
    """
    compiled: list[SetAssignment] = []
    for column, value in (assignments or {}).items():
        validate_query_column(model_cls, column)
        _reject_kwargs_value_expr(value)
        compiled.append(_guarded_literal_assignment(model_cls, column, value))
    return tuple(compiled)


def _relation_names(model_cls: type) -> set[str]:
    relations = getattr(model_cls, "__ferro_relation_specs__", None) or {}
    reverse = getattr(model_cls, "__ferro_reverse_specs__", None) or {}
    return set(relations) | set(reverse)


def _reject_relation(model_cls: type, name: str, *, role: str) -> None:
    if name in _relation_names(model_cls):
        raise TypeError(
            f"update() cannot assign a relation {name!r} as a SET {role}; "
            "targets and sources must be root columns on the updated table"
        )


def _column_family(model_cls: type, column: str) -> str:
    spec = getattr(model_cls, "__ferro_columns__", {})[column]
    return _aggregate_source_family(spec)


def _column_ref_value(
    model_cls: type, target: str, value: FieldProxy
) -> ColumnRefValueExpr:
    if value.path:
        raise TypeError(
            f"update() cannot assign a traversed column "
            f"({value._dotted()}); SET targets and sources must be "
            "root columns on the updated table"
        )
    _reject_relation(model_cls, value.column, role="value")
    validate_query_column(model_cls, value.column)
    source_family = _column_family(model_cls, value.column)
    target_family = _column_family(model_cls, target)
    if source_family != target_family:
        model = model_cls.__name__
        raise TypeError(
            f"cannot copy {model}.{value.column} ({source_family}) "
            f"onto {model}.{target} ({target_family})"
        )
    return ColumnRefValueExpr(column=value.column)


def _compile_operand(model_cls: type, target: str, value: Any) -> SetValueExpr:
    """Compile one side of a binary SET expression (#378)."""
    if isinstance(value, (RelationProxy, ReverseRelationProxy)):
        relation = value._path[-1] if isinstance(value, RelationProxy) else value._name
        raise TypeError(
            f"update() cannot assign the relation {relation!r} as a SET "
            "value; sources must be root columns on the updated table"
        )
    if isinstance(value, NowExpr) or value is now:
        raise TypeError(
            "now is the database clock, not a numeric operand; "
            "+ / - take a numeric column (int, float, or Decimal)"
        )
    if isinstance(value, BinaryValueExpr):
        return _compile_binary(model_cls, target, value)
    if isinstance(value, MergeValueExpr):
        raise TypeError(
            ".merge() cannot be nested inside + / -; it is a SET value, "
            "not an arithmetic operand"
        )
    if isinstance(value, ValueExpr):
        raise TypeError(
            "update() recipe values are a literal, a root column copy, "
            "arithmetic, now, or .merge(); .concat() is not shipped yet"
        )
    if isinstance(value, FieldProxy):
        if value.path:
            raise TypeError(
                f"update() cannot assign a traversed column "
                f"({value._dotted()}); SET targets and sources must be "
                "root columns on the updated table"
            )
        _reject_relation(model_cls, value.column, role="value")
        validate_query_column(model_cls, value.column)
        source_family = _column_family(model_cls, value.column)
        if source_family not in _AGG_NUMERIC:
            model = model_cls.__name__
            raise TypeError(
                f"{value._dotted()} is not supported as an arithmetic "
                f"operand: {model}.{value.column} is {source_family}-typed, "
                "and + / - take a numeric column (int, float, or Decimal)."
            )
        return ColumnRefValueExpr(column=value.column)
    return _literal_assignment(target, value).value


def _compile_binary(
    model_cls: type, target: str, value: BinaryValueExpr
) -> BinarySetExpr:
    target_family = _column_family(model_cls, target)
    if target_family not in _AGG_NUMERIC:
        model = model_cls.__name__
        raise TypeError(
            f"cannot assign a numeric expression onto {model}.{target} "
            f"({target_family})"
        )
    return BinarySetExpr(
        op="add" if value.op == "+" else "sub",
        left=_compile_operand(model_cls, target, value.left),
        right=_compile_operand(model_cls, target, value.right),
    )


def _column_json_shape(model_cls: type, column: str) -> str | None:
    spec = getattr(model_cls, "__ferro_columns__", {})[column]
    return _json_column_shape(spec)


def _require_postgres_merge(using: str | None) -> None:
    """Reject ``.merge()`` when the connected dialect is SQLite (#379)."""
    from .._core import connection_backend

    if connection_backend(using) == "sqlite":
        raise TypeError(
            ".merge() is Postgres-only (jsonb ||). SQLite is not supported; "
            "json_patch would delete keys on JSON null (RFC 7396), which is "
            "a different meaning."
        )


def _compile_merge(
    model_cls: type, target: str, value: MergeValueExpr, using: str | None
) -> MergeSetExpr:
    _require_postgres_merge(using)
    target_shape = _column_json_shape(model_cls, target)
    if target_shape == "array":
        raise TypeError(
            f"cannot assign .merge() onto {model_cls.__name__}.{target}: "
            "it is list-typed; .concat() is not shipped yet"
        )
    if target_shape != "object":
        family = _column_family(model_cls, target)
        raise TypeError(
            f".merge() can only be assigned to a json object column; "
            f"{model_cls.__name__}.{target} is {family}-typed"
        )
    left = value.left
    if left.path:
        raise TypeError(
            f"update() cannot assign a traversed column "
            f"({left._dotted()}); SET targets and sources must be "
            "root columns on the updated table"
        )
    _reject_relation(model_cls, left.column, role="value")
    validate_query_column(model_cls, left.column)
    source_shape = _column_json_shape(model_cls, left.column)
    if source_shape == "array":
        raise TypeError(
            f"{left._dotted()}.merge() is not supported: "
            f"{model_cls.__name__}.{left.column} is list-typed; "
            ".concat() is not shipped yet"
        )
    if source_shape != "object":
        family = _column_family(model_cls, left.column)
        raise TypeError(
            f"{left._dotted()}.merge() is not supported: "
            f"{model_cls.__name__}.{left.column} is {family}-typed, "
            "and .merge() takes a json object (dict or nested model)."
        )
    return MergeSetExpr(
        left=ColumnRefValueExpr(column=left.column),
        right=_literal_assignment(target, value.patch).value,
    )


def _recipe_set(
    model_cls: type,
    recipe: Mapping[str, Any],
    using: str | None = None,
) -> tuple[SetAssignment, ...]:
    """Compile a recipe dict into the same SET list kwargs use."""
    compiled: list[SetAssignment] = []
    for column, value in recipe.items():
        if not isinstance(column, str):
            raise TypeError(
                "update() recipe keys are root column names and must be "
                f"strings, got {column!r} ({type(column).__name__})"
            )
        _reject_relation(model_cls, column, role="target")
        validate_query_column(model_cls, column)
        if isinstance(value, (RelationProxy, ReverseRelationProxy)):
            relation = (
                value._path[-1] if isinstance(value, RelationProxy) else value._name
            )
            raise TypeError(
                f"update() cannot assign the relation {relation!r} as a SET "
                "value; sources must be root columns on the updated table"
            )
        if isinstance(value, NowExpr) or value is now:
            family = _column_family(model_cls, column)
            if family != "datetime":
                raise TypeError(
                    f"now can only be assigned to a datetime column; "
                    f"{model_cls.__name__}.{column} is {family}-typed"
                )
            compiled.append(SetAssignment(column=column, value=NowSetExpr()))
            continue
        if isinstance(value, BinaryValueExpr):
            compiled.append(
                SetAssignment(
                    column=column,
                    value=_compile_binary(model_cls, column, value),
                )
            )
            continue
        if isinstance(value, MergeValueExpr):
            compiled.append(
                SetAssignment(
                    column=column,
                    value=_compile_merge(model_cls, column, value, using),
                )
            )
            continue
        if isinstance(value, ValueExpr):
            raise TypeError(
                "update() recipe values are a literal, a root column copy, "
                "arithmetic, now, or .merge(); .concat() is not shipped yet"
            )
        if isinstance(value, FieldProxy):
            compiled.append(
                SetAssignment(
                    column=column,
                    value=_column_ref_value(model_cls, column, value),
                )
            )
            continue
        compiled.append(_guarded_literal_assignment(model_cls, column, value))
    return tuple(compiled)


def _after_query_values(position: tuple[Any, ...]) -> tuple[QueryValue, ...]:
    """Typed query-value nodes for one position bound (same shape as WHERE leaves)."""
    values: list[QueryValue] = []
    for item in position:
        serialized = canonicalize_wire_scalar(item)
        values.append(QueryValue(kind=_query_value_kind(serialized), value=serialized))
    return tuple(values)


def _compile_position(
    query: "Query[Any]", bound: tuple[Any, ...] | None, *, op: str
) -> tuple[QueryValue, ...] | None:
    """Serialize a fetch payload's ``after`` / ``before`` bound."""
    if bound is None:
        return None
    query._assert_position_order_keys()
    if len(bound) != len(query.order_by_clause):
        raise ValueError(
            f"{op}() expected {len(query.order_by_clause)} position values "
            f"(one per order_by key), got {len(bound)}"
        )
    return _after_query_values(bound)


def _compile_after(query: "Query[Any]") -> tuple[QueryValue, ...] | None:
    """Serialize a fetch payload's ``after`` bound, re-checking current order keys."""
    return _compile_position(query, query._after, op="after")


def _compile_before(query: "Query[Any]") -> tuple[QueryValue, ...] | None:
    """Serialize a fetch payload's ``before`` bound, re-checking current order keys."""
    return _compile_position(query, query._before, op="before")


def _reject_mutation_misuse(query: "Query[Any]", operation: str) -> None:
    """Reject query state a mutating verb cannot carry (FF-A A1, #273, #287).

    Raises:
        ValueError: If ``limit()``/``offset()``/``after()``/``before()`` was set, the query
            traverses a relation (a joined/explicit-edge path, or a where-clause
            leaf carrying a non-empty path — portable SQL has no
            ``UPDATE/DELETE ... JOIN``; a join-free shadow-FK filter stays
            allowed), or the query carries ``include()``. Rejected here, before
            any DB round-trip (the Rust guard from #270 stays as boundary
            defense).
    """
    if (
        query._limit is not None
        or query._offset is not None
        or query._after is not None
        or query._before is not None
    ):
        raise ValueError(
            f"{operation}() does not support limit/offset/after/before: portable SQL "
            f"has no {operation.upper()} ... LIMIT. Remove the "
            f".limit()/.offset()/.after()/.before() call, or fetch primary keys first "
            f"and {operation} by primary-key set."
        )
    if (
        query._joins
        or query._explicit_edges
        or any(_where_node_traverses(node) for node in query.where_clause)
    ):
        raise ValueError(
            f"{operation}() does not support relation traversal: portable SQL "
            f"has no {operation.upper()} ... JOIN. Fetch primary keys via the "
            f"joined query first, then {operation} by primary-key set. "
            "(A join-free relation filter like `t.account == instance` is "
            "allowed.)"
        )
    if query._includes:
        raise ValueError(
            f"{operation}() does not support include(): a mutation is a "
            "single-table write shape and returns no instances to "
            "populate. Drop the .include(...) call, or fetch the "
            "populated rows first and mutate by primary-key set."
        )


@dataclass(frozen=True)
class CompiledQuery:
    """A compiled query: its payload, the JSON sent and the related model classes.

    The JSON and the model classes both come from the one payload, so they
    always agree.
    """

    payload: QueryIrPayload
    """The query's payload."""
    wire_json: str
    """The payload serialized as the JSON document sent to the core."""
    hop_classes: dict[str, type] | None
    """The related model class for each joined table, keyed by table name.

    `None` unless the results are built through related models: with
    `include()`, or a projection that reads a related model's column.
    """
    secret_values: tuple[object, ...] = ()
    """What each `SecretStr` / `SecretBytes` literal the query compiled with
    hides, unwrapped, in the order the compile met them; empty when there is
    none. It travels beside the payload, never inside it: the statement log
    masks a bound parameter equal to any of these."""


def _collect_node_literals(node: QueryNode, out: list[object]) -> None:
    """Append the literals of one predicate tree, depth first."""
    if node.child is not None:
        _collect_node_literals(node.child, out)
    elif node.exists is not None:
        for inner in node.exists.where:
            _collect_node_literals(inner, out)
    elif node.is_compound:
        if node.left is not None:
            _collect_node_literals(node.left, out)
        if node.right is not None:
            _collect_node_literals(node.right, out)
    else:
        out.append(node.value)


def _secret_literals(
    query: "Query[Any]",
    verb: QueryVerb,
    assignments: Mapping[str, Any] | None,
    recipe: Mapping[str, Any] | None,
) -> tuple[object, ...]:
    """What every secret-typed literal the compile unwraps hides.

    Read from the same values the payload compiles from (predicate literals,
    the SET values of an update, a fetch's position bounds), so the set and
    the wire cannot disagree about which literals were secrets, and collected
    in one call to the one collector.
    """
    literals: list[object] = []
    for node in query.where_clause:
        _collect_node_literals(node, literals)
    if verb == "update":
        sets = recipe if recipe is not None else assignments or {}
        literals.extend(sets.values())
    if verb == "fetch":
        for bound in (query._after, query._before):
            literals.extend(bound or ())
    return _bind_payload.secret_values_in(literals)


def _payload_hop_classes(payload: QueryIrPayload) -> dict[str, type] | None:
    """Collect the plan-scoped hop-class map from the payload's own hops."""
    materialization = payload.materialization
    needs = isinstance(materialization, Instances) or (
        isinstance(materialization, Record)
        and any(field.expr is None and field.path for field in materialization.fields)
    )
    if not needs:
        return None
    hop_classes: dict[str, type] = {}
    for join in payload.joins:
        for hop in join.path:
            hop_classes[hop.to_table] = hop.target
    if isinstance(materialization, Instances):
        for path in materialization.paths:
            for hop in path:
                hop_classes[hop.to_table] = hop.target
    return hop_classes


def compile_query(
    query: "Query[Any]",
    verb: QueryVerb,
    *,
    assignments: Mapping[str, Any] | None = None,
    recipe: Mapping[str, Any] | None = None,
) -> CompiledQuery:
    """Compile a built query into the payload for one operation.

    Each operation carries a different part of the query:

    - `"fetch"` carries everything: sort keys, paging, joins and how the
      results are built.
    - `"count"` ignores sort keys, paging and any projection, and keeps the
      conditions and joins that decide which rows match.
    - `"update"` and `"delete"` write one table: paging, relation paths and
      `include()` are refused, and the payload leaves out the paging keys.

    Args:
        query: The built query.
        verb: The operation: `"fetch"`, `"count"`, `"update"` or `"delete"`.
        assignments: The literal column values of a keyword `update()`.
            Defaults to `None`.
        recipe: The column values an `update()` recipe returned. Defaults to
            `None`.

    Returns:
        The payload, its JSON and the related model classes.

    Raises:
        ValueError: An update or delete has paging, a relation path or an
            `include()`, or a recipe value is not allowed.
        TypeError: Both `assignments` and `recipe` are given, or a value
            cannot be assigned.
    """
    identity = model_identity(query.model_cls)
    where = tuple(node.to_ir_dict() for node in query.where_clause)
    if verb in ("update", "delete"):
        _reject_mutation_misuse(query, verb)
        if verb == "update":
            if recipe is not None and assignments:
                raise TypeError(
                    "compile_query() accepts either recipe or assignments, not both"
                )
            set_assignments = (
                _recipe_set(
                    query.model_cls,
                    recipe,
                    using=resolve_compile_connection_name(
                        using=query._using, session=query._session
                    ),
                )
                if recipe is not None
                else _literal_set(query.model_cls, assignments)
            )
        else:
            set_assignments = ()
        payload = QueryIrPayload(
            model_name=identity,
            where=where,
            set=set_assignments,
            order_by=(),
            limit=_ABSENT,
            offset=_ABSENT,
            after=None,
            before=None,
            m2m=None,
            joins=(),
            materialization=RootInstances(),
        )
    elif verb == "count":
        payload = QueryIrPayload(
            model_name=identity,
            where=where,
            set=(),
            order_by=(),
            limit=None,
            offset=None,
            after=None,
            before=None,
            m2m=query._m2m_context,
            joins=_serialize_joins(query),
            materialization=RootInstances(),
        )
    else:
        payload = QueryIrPayload(
            model_name=identity,
            where=where,
            set=(),
            order_by=tuple(query.order_by_clause),
            limit=query._limit,
            offset=query._offset,
            after=_compile_after(query),
            before=_compile_before(query),
            m2m=query._m2m_context,
            joins=_serialize_joins(query),
            materialization=_materialization(query),
        )
    return CompiledQuery(
        payload=payload,
        wire_json=to_wire_json(payload),
        hop_classes=_payload_hop_classes(payload),
        secret_values=_secret_literals(query, verb, assignments, recipe),
    )


def to_wire_json(payload: QueryIrPayload) -> str:
    """Serialize a payload as the versioned JSON document sent to the core.

    Args:
        payload: The payload to serialize.

    Returns:
        The JSON text, with the payload under `"payload"` beside its kind and
            version.
    """
    return json.dumps(
        {
            "ir_kind": "query",
            "ir_version": _IR_VERSION,
            "payload": canonicalize_wire_scalar(payload.to_ir_dict()),
        }
    )
