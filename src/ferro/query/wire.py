"""The QueryIR payload: Ferro's single query wire contract (CONTEXT.md).

Every query the ORM executes crosses the Python→Rust seam as one versioned
JSON envelope. This module is the only place that wire shape exists on the
Python side: :func:`compile_query` is the sole compiler from a built query to
a :class:`QueryIrPayload`, and :func:`to_wire_json` the sole envelope
serializer. The dataclasses mirror ``crates/ferro-schema-ir``'s serde structs
one-for-one; golden vectors in ``tests/fixtures/ir_vectors/`` pin both sides
to the same bytes (builder equality in ``tests/test_query_wire_vectors.py``,
serde round-trips in the Rust crate's tests).

Friend contract: :func:`compile_query` reads the query object's build state
(``where_clause``, ``order_by_clause``, ``_limit``, ``_offset``, ``_joins``,
``_explicit_edges``, ``_includes``, ``_m2m_context``, ``_projection``)
directly — the builder and this module are one package, and the seam is
``compile_query``, not a snapshot type. The builder owns chainer-time
validation and state; this module owns everything whose output is wire shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping

from .._bind_payload import update_bind_payload
from .nodes import (
    FieldProxy,
    QueryNode,
    RelationProxy,
    ReverseRelationProxy,
    ValueExpr,
    _aggregate_source_family,
    _query_value_kind,
    _serialize_query_value,
    validate_query_column,
)

if TYPE_CHECKING:
    from .builder import Query

# The verb a payload is compiled for. ``update`` and ``delete`` share the
# mutate arm; they stay distinct values so guardrail errors name the caller.
QueryVerb = Literal["fetch", "count", "update", "delete"]

# Always 9 (#377 — unconditional bump, exactly like v8 at #376 and v7 at
# #314). v9 adds the column-ref SET value-expression kind beside literal.
# Python and Rust ship in one wheel, so a single supported version is the
# whole contract.
_IR_VERSION = 9


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
    """Qualified registry identity of a Ferro model class (FF-E)."""
    return model_cls.__ferro_identity__  # ty: ignore[unresolved-attribute]


def path_edges(path: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Every edge (prefix of length ≥ 1) of a relation ``path`` (#272)."""
    return [path[:i] for i in range(1, len(path) + 1)]


@dataclass(frozen=True)
class QueryJoinHop:
    """One hop fact of a relation path — the shared shape of the ``joins``
    section, ``instances`` plan paths, and aggregate ``expr`` traversals.

    ``target`` is the hop's model class — a compile-side fact riding the hop
    (the hop-class map is collected from it), never serialized: the wire
    carries ``to_table`` and Rust resolves registration facts from its own
    registry.
    """

    relation: str
    from_column: str
    to_table: str
    to_column: str
    target: type = field(compare=False, repr=False, kw_only=True)

    def to_ir_dict(self) -> dict[str, str]:
        return {
            "relation": self.relation,
            "from_column": self.from_column,
            "to_table": self.to_table,
            "to_column": self.to_column,
        }


@dataclass(frozen=True)
class QueryJoin:
    """One ``joins`` entry: a full relation path and its resolved join type."""

    join_type: str
    path: tuple[QueryJoinHop, ...]

    def to_ir_dict(self) -> dict[str, Any]:
        return {
            "join_type": self.join_type,
            "path": [hop.to_ir_dict() for hop in self.path],
        }


@dataclass(frozen=True)
class OrderByEntry:
    """One ``ORDER BY`` clause: column, direction, relation path, and nulls."""

    column: str
    direction: str
    path: tuple[str, ...]
    nulls: str | None = None

    def to_ir_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "column": self.column,
            "direction": self.direction,
            "path": list(self.path),
        }
        if self.nulls is not None:
            payload["nulls"] = self.nulls
        return payload


@dataclass(frozen=True)
class M2mContext:
    """Many-to-many linkage context riding the payload's ``m2m`` section."""

    join_table: str
    source_col: str
    target_col: str
    source_id: Any

    def to_ir_dict(self) -> dict[str, Any]:
        return {
            "join_table": self.join_table,
            "source_col": self.source_col,
            "target_col": self.target_col,
            "source_id": self.source_id,
        }


@dataclass(frozen=True)
class QueryValue:
    """One typed literal value carried by a value-expression node."""

    kind: str
    value: Any

    def to_ir_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class LiteralValueExpr:
    """A bound literal in the clause-agnostic SET value-expression family."""

    value: QueryValue

    def to_ir_dict(self) -> dict[str, Any]:
        return {"kind": "literal", "value": self.value.to_ir_dict()}


@dataclass(frozen=True)
class ColumnRefValueExpr:
    """An unqualified root-column copy in the SET value-expression family."""

    column: str

    def to_ir_dict(self) -> dict[str, Any]:
        return {"kind": "column", "column": self.column}


@dataclass(frozen=True)
class SetAssignment:
    """One ordered root-column assignment in the canonical SET section."""

    column: str
    value: LiteralValueExpr | ColumnRefValueExpr

    def to_ir_dict(self) -> dict[str, Any]:
        return {"column": self.column, "value": self.value.to_ir_dict()}


@dataclass(frozen=True)
class RecordExpr:
    """The expression source of an aggregate record field (v5, ADR-0009).

    Self-contained on the wire: ``fn`` travels as data (the closed aggregate
    set) and ``path`` carries the source column's traversal as ordered hop
    facts rather than keying into the ``joins`` section.
    """

    fn: str
    column: str
    path: tuple[QueryJoinHop, ...]

    def to_ir_dict(self) -> dict[str, Any]:
        return {
            "fn": self.fn,
            "column": self.column,
            "path": [hop.to_ir_dict() for hop in self.path],
        }


@dataclass(frozen=True)
class RecordField:
    """One projected field of a ``record`` plan (ADR-0007/ADR-0009).

    ``name`` is declared separately from the source (output aliases are
    free). A field reads from exactly one source: a ``column`` + relation
    ``path``, or an aggregate ``expr`` whose traversal rides the expression —
    never both, never neither (the Rust side enforces this at
    deserialization).
    """

    name: str
    column: str | None
    path: tuple[str, ...]
    expr: RecordExpr | None

    def to_ir_dict(self) -> dict[str, Any]:
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
    """Complete root-model instances — the default materialization plan."""

    def to_ir_dict(self) -> dict[str, Any]:
        return {"kind": "root_instances"}


@dataclass(frozen=True)
class Record:
    """Projected records: a typed column subset decoded into ``Row`` records."""

    fields: tuple[RecordField, ...]

    def to_ir_dict(self) -> dict[str, Any]:
        return {
            "kind": "record",
            "fields": [field.to_ir_dict() for field in self.fields],
        }


@dataclass(frozen=True)
class Instances:
    """Populated instance graphs (joined-row hydration, ADR-0008): one
    ordered hop-fact list per include path, deliberately separate from the
    ``joins`` section so include can never change membership or a shared
    edge's join type."""

    paths: tuple[tuple[QueryJoinHop, ...], ...]

    def to_ir_dict(self) -> dict[str, Any]:
        return {
            "kind": "instances",
            "paths": [[hop.to_ir_dict() for hop in path] for path in self.paths],
        }


Materialization = RootInstances | Record | Instances


@dataclass(frozen=True)
class QueryIrPayload:
    """The single typed wire artifact a query ships to the Rust runtime.

    Mirrors ``ferro_schema_ir::QueryIrPayload`` field-for-field. ``where``
    holds :meth:`QueryNode.to_ir_dict` output — the node module owns the
    predicate wire shape. ``limit``/``offset`` may be :data:`_ABSENT`
    (mutating payloads omit the keys entirely).
    """

    model_name: str
    where: tuple[dict[str, Any], ...]
    set: tuple[SetAssignment, ...]
    order_by: tuple[OrderByEntry, ...]
    limit: int | None | _AbsentType
    offset: int | None | _AbsentType
    m2m: M2mContext | None
    joins: tuple[QueryJoin, ...]
    materialization: Materialization

    def to_ir_dict(self) -> dict[str, Any]:
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
    """Resolve a relation path into ordered hop facts.

    Each hop's ``relation``/``from_column``/``to_table``/``to_column`` is read
    from the relation-spec chain starting at ``model_cls`` — ``from_column`` is
    the shadow FK column on the source side, ``to_column`` the target's PK.

    Raises:
        ValueError: If a path element is not a declared relation of the current
            model (should not happen — proxies validate at build time), or the
            target has no single primary key (:func:`_target_pk_column`).
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
    carrying its ordered hop facts (:func:`resolve_join_hops`). The Rust
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


def _literal_assignment(column: str, value: Any) -> SetAssignment:
    """Canonicalize one literal (including bytes) into a SET assignment."""
    payload = update_bind_payload({column: value})
    canon = payload[column]
    if isinstance(canon, bytes):
        query_value = QueryValue(kind="bytes", value=list(canon))
    else:
        query_value = QueryValue(kind=_query_value_kind(canon), value=canon)
    return SetAssignment(column=column, value=LiteralValueExpr(value=query_value))


def _literal_set(assignments: Mapping[str, Any] | None) -> tuple[SetAssignment, ...]:
    """Compile kwargs literals into ordered, typed SET value-expression nodes."""
    compiled: list[SetAssignment] = []
    for column, value in (assignments or {}).items():
        _reject_kwargs_value_expr(value)
        compiled.append(_literal_assignment(column, value))
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


def _recipe_set(
    model_cls: type, recipe: Mapping[str, Any]
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
        if isinstance(value, ValueExpr):
            raise TypeError(
                "update() recipe values are a literal or a root column copy; "
                "other value expressions land in #378 / #379"
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
            target_family = _column_family(model_cls, column)
            if source_family != target_family:
                model = model_cls.__name__
                raise TypeError(
                    f"cannot copy {model}.{value.column} ({source_family}) "
                    f"onto {model}.{column} ({target_family})"
                )
            compiled.append(
                SetAssignment(
                    column=column,
                    value=ColumnRefValueExpr(column=value.column),
                )
            )
            continue
        compiled.append(_literal_assignment(column, value))
    return tuple(compiled)


def _reject_mutation_misuse(query: "Query[Any]", operation: str) -> None:
    """Reject query state a mutating verb cannot carry (FF-A A1, #273, #287).

    Raises:
        ValueError: If ``limit()``/``offset()`` was set, the query traverses
            a relation (a joined/explicit-edge path, or a where-clause leaf
            carrying a non-empty path — portable SQL has no ``UPDATE/DELETE
            ... JOIN``; a join-free shadow-FK filter stays allowed), or the
            query carries ``include()``. Rejected here, before any DB
            round-trip (the Rust guard from #270 stays as boundary defense).
    """
    if query._limit is not None or query._offset is not None:
        raise ValueError(
            f"{operation}() does not support limit/offset: portable SQL has "
            f"no {operation.upper()} ... LIMIT. Remove the .limit()/.offset() "
            f"call, or fetch primary keys first and {operation} by "
            "primary-key set."
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
    """One compiled artifact per query — CONTEXT.md "Compiled query".

    ``wire_json`` and ``hop_classes`` are two views of the same compile:
    the map is collected from the hop facts the payload itself carries, so
    the wire and the classes the runtime hydrates through can never
    disagree (the builder previously re-walked relation-spec chains to
    assemble ``hop_classes`` beside the wire, agreeing by hand).

    ``hop_classes`` is plan-scoped, mirroring the Rust ``needs_hop_classes``
    guard exactly (a both-sides double-check, like #272 join edges): ``None``
    unless the materialization plan decodes or hydrates through a hop model's
    class — an ``instances`` plan (#286) or a ``record`` plan with traversed
    plain fields (#293). When it is a map, it covers every hop the payload
    carries, keyed by physical table (one model per table, enforced at
    registration).
    """

    payload: QueryIrPayload
    wire_json: str
    hop_classes: dict[str, type] | None


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
    """Compile a built query into its wire artifact — the single choke point.

    What each verb carries is policy, stated here once:

    - ``fetch`` carries everything: ordering, paging, m2m context, joins,
      and the query's own materialization plan.
    - ``count`` is unaffected by ordering, paging, and projection (PRD #277
      verb table): it materializes a scalar, so ordering is dropped, paging
      is ``null``, and the plan is ``root_instances`` even on a projected
      query — joins and the m2m context still shape membership.
    - ``update``/``delete`` are single-table write shapes: guardrails reject
      paging, traversal, and includes (:func:`_reject_mutation_misuse`), and
      the payload omits the paging keys entirely.

    Raises:
        ValueError: From the mutate guardrails.
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
                _recipe_set(query.model_cls, recipe)
                if recipe is not None
                else _literal_set(assignments)
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
            m2m=query._m2m_context,
            joins=_serialize_joins(query),
            materialization=_materialization(query),
        )
    return CompiledQuery(
        payload=payload,
        wire_json=to_wire_json(payload),
        hop_classes=_payload_hop_classes(payload),
    )


def to_wire_json(payload: QueryIrPayload) -> str:
    """Serialize a payload into the versioned IR envelope JSON string."""
    return json.dumps(
        {
            "ir_kind": "query",
            "ir_version": _IR_VERSION,
            "payload": _serialize_query_value(payload.to_ir_dict()),
        }
    )
