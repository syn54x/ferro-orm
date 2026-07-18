"""SchemaIR compilation and fingerprint helpers for Phase 1.

This module compiles column specs (:class:`ferro.columns.ColumnSpec`) into
RFC-shaped SchemaIR envelopes and persists deterministic fingerprints for
individual models and full model sets.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .._core import (
    _ddl_check_constraint_name,
    _ddl_composite_index_name,
    _ddl_composite_unique_name,
    _ddl_fk_name,
    _ddl_single_index_name,
    _ddl_single_unique_name,
)
from ..columns import (
    ColumnSpec,
    build_column_specs,
    build_relation_specs,
    build_reverse_specs,
    primary_key_field_name,
)
from ..composite_indexes import drop_overlap_with_uniques, normalized_composite_indexes
from ..composite_uniques import normalized_composite_uniques
from ..registry import REGISTRY, ir_fingerprint

_IR_VERSION = 1

# Test-only counter bumped at the single SchemaIR compile choke point (#245).
_SCHEMA_IR_COMPILE_COUNT_FOR_TEST = 0


def schema_ir_compile_count_for_test() -> int:
    """Return how many per-model SchemaIR compiles have run (test instrument)."""
    return _SCHEMA_IR_COMPILE_COUNT_FOR_TEST


def reset_schema_ir_compile_count_for_test() -> None:
    """Reset the compile counter (``clean_registry`` fixture)."""
    global _SCHEMA_IR_COMPILE_COUNT_FOR_TEST
    _SCHEMA_IR_COMPILE_COUNT_FOR_TEST = 0


def _column_ir_from_spec(spec: ColumnSpec) -> dict[str, Any]:
    """Render one ColumnSpec into the locked SchemaIR ``columns[]`` shape."""
    column_ir: dict[str, Any] = {
        "name": spec.name,
        "logical_type": spec.logical_type,
        "nullable": spec.nullable,
        "primary_key": spec.primary_key,
        "autoincrement": spec.autoincrement,
        "unique": spec.unique,
        "index": spec.index,
        "default": spec.default,
        "format": spec.format,
    }
    if spec.enum_values is not None:
        column_ir["enum_values"] = list(spec.enum_values)
    if spec.db_type_explicit:
        column_ir["db_type"] = spec.db_type
        column_ir["db_type_explicit"] = True
    if spec.enum_type_name:
        column_ir["enum_type_name"] = spec.enum_type_name
    return column_ir


# Artifact names come from the single-source builders in ferro-ddl-lowering
# (via _core FFI) — including the 63-char truncation guards the old hand-rolled
# single-column helpers lacked. Do not re-implement name formats here
# (AGENTS.md § I-1; FF-B B3).


def _fk_name(table_name: str, col_name: str, to_table: str) -> str:
    """Canonical foreign-key constraint name (shared Rust builder)."""
    return _ddl_fk_name(table_name, col_name, to_table)


def _single_index_name(table_name: str, col_name: str) -> str:
    """Canonical single-column index name (shared Rust builder)."""
    return _ddl_single_index_name(table_name, col_name)


def _single_unique_name(table_name: str, col_name: str) -> str:
    """Canonical single-column unique name (shared Rust builder)."""
    return _ddl_single_unique_name(table_name, col_name)


def _composite_index_name(table_name: str, columns: list[str]) -> str:
    """Canonical composite index name (shared Rust builder)."""
    return _ddl_composite_index_name(table_name, columns)


def _composite_unique_name(table_name: str, columns: list[str]) -> str:
    """Canonical composite unique name (shared Rust builder)."""
    return _ddl_composite_unique_name(table_name, columns)


def compile_schema_ir_payload(
    model_name: str,
    columns: Sequence[ColumnSpec],
    *,
    table_name: str | None = None,
    composite_uniques: Sequence[Sequence[str]] = (),
    composite_indexes: Sequence[Sequence[str]] = (),
) -> dict[str, Any]:
    """Compile column specs into a SchemaIR payload object (locked shape).

    Args:
        model_name: Registered model class name.
        columns: One ColumnSpec per column.
        table_name: Physical table name (defaults to ``model_name.lower()``).
        composite_uniques: Validated composite-unique column groups.
        composite_indexes: Validated composite-index column groups.

    Returns:
        A SchemaIR payload object ready to be wrapped in an IR envelope.
    """
    resolved_table_name = table_name or model_name.lower()
    # Collapse same-named specs, last one wins — mirrors the historical
    # ``properties`` dict-literal merge (a self-referential M2M join table
    # derives an identical ``{table}_id`` name for both its source and target
    # shadow-FK columns; the dict pipeline silently collapsed that duplicate
    # key, so specs must too, or the compiled table gains a duplicate column).
    deduped: dict[str, ColumnSpec] = {}
    for spec in columns:
        deduped[spec.name] = spec
    ordered = sorted(deduped.values(), key=lambda spec: spec.name)

    column_entries = [_column_ir_from_spec(spec) for spec in ordered]

    foreign_keys: list[dict[str, Any]] = []
    indexes: list[dict[str, Any]] = []
    uniques: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    for spec in ordered:
        if spec.foreign_key is not None and spec.foreign_key.to_table:
            foreign_keys.append(
                {
                    "column": spec.name,
                    "to_table": spec.foreign_key.to_table,
                    "to_column": "id",
                    "on_delete": spec.foreign_key.on_delete,
                    "name": _fk_name(
                        resolved_table_name, spec.name, spec.foreign_key.to_table
                    ),
                }
            )
        if spec.index:
            indexes.append(
                {
                    "name": _single_index_name(resolved_table_name, spec.name),
                    "columns": [spec.name],
                    "unique": False,
                }
            )
        if spec.unique:
            uniques.append(
                {
                    "name": _single_unique_name(resolved_table_name, spec.name),
                    "columns": [spec.name],
                }
            )
        if spec.db_check and spec.enum_values:
            rendered: list[str] = []
            for value in spec.enum_values:
                if isinstance(value, bool):
                    rendered.append(str(value).lower())
                elif isinstance(value, (int, float)):
                    rendered.append(str(value))
                else:
                    escaped = str(value).replace("'", "''")
                    rendered.append(f"'{escaped}'")
            checks.append(
                {
                    "name": _ddl_check_constraint_name(resolved_table_name, spec.name),
                    "column": spec.name,
                    "values": rendered,
                }
            )

    for cols in composite_indexes:
        indexes.append(
            {
                "name": _composite_index_name(resolved_table_name, list(cols)),
                "columns": list(cols),
                "unique": False,
            }
        )
    for cols in composite_uniques:
        uniques.append(
            {"name": _composite_unique_name(resolved_table_name, list(cols)), "columns": list(cols)}
        )

    model_payload = {
        "model_name": model_name,
        "table_name": resolved_table_name,
        "columns": column_entries,
        "foreign_keys": sorted(
            foreign_keys,
            key=lambda item: (item["column"], item["to_table"], item["to_column"]),
        ),
        "indexes": sorted(indexes, key=lambda item: item["name"]),
        "uniques": sorted(uniques, key=lambda item: item["name"]),
        "checks": sorted(checks, key=lambda item: item["name"]),
    }
    return {"dialect_agnostic": True, "models": [model_payload]}


def _persist_schema_ir_envelope(model_name: str, envelope: dict[str, Any]) -> None:
    """Store one model's compiled SchemaIR envelope and fingerprint.

    Routes through the Registry's envelope-write entrypoint so every per-model
    store mutation lives at the store-owning layer (#243); the fingerprint is
    derived from the envelope inside that entrypoint.
    """
    REGISTRY.persist_envelope(model_name, envelope)


def _compile_and_persist_model_envelope(
    model_name: str,
    columns: Sequence[ColumnSpec],
    *,
    table_name: str | None = None,
    composite_uniques: Sequence[Sequence[str]] = (),
    composite_indexes: Sequence[Sequence[str]] = (),
) -> dict[str, Any]:
    """Compile one model or join table to SchemaIR and persist its envelope.

    Single compile choke point for the test instrument and for every producer
    of per-model envelopes (#245).
    """
    global _SCHEMA_IR_COMPILE_COUNT_FOR_TEST
    _SCHEMA_IR_COMPILE_COUNT_FOR_TEST += 1
    payload = compile_schema_ir_payload(
        model_name,
        columns,
        table_name=table_name,
        composite_uniques=composite_uniques,
        composite_indexes=composite_indexes,
    )
    envelope = wrap_schema_ir(payload)
    _persist_schema_ir_envelope(model_name, envelope)
    return envelope


def register_model_with_ir(
    model_name: str,
    columns: Sequence[ColumnSpec],
    table_name: str,
    *,
    composite_uniques: Sequence[Sequence[str]] = (),
    composite_indexes: Sequence[Sequence[str]] = (),
) -> dict[str, Any]:
    """Compile SchemaIR once from column specs and persist the envelope.

    Single registration bundle for join tables and any other producer that
    already has fully-formed column specs in hand (#236).
    """
    return _compile_and_persist_model_envelope(
        model_name,
        columns,
        table_name=table_name,
        composite_uniques=composite_uniques,
        composite_indexes=composite_indexes,
    )


def wrap_schema_ir(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a SchemaIR payload with the standard IR envelope fields."""
    return {
        "ir_kind": "schema",
        "ir_version": _IR_VERSION,
        "payload": payload,
    }


def compile_model_schema_ir(
    model_name: str, model_cls: type[Any], specs: dict[str, ColumnSpec] | None = None
) -> dict[str, Any]:
    """Build (or accept) specs for one model, refresh the class attr, register.

    The single recompile source: metaclass registration, relationship
    resolution's second pass, envelope re-registration, and the dirty
    recompile paths all come through here, so ``__ferro_columns__`` can never
    go stale relative to the persisted envelope.

    Args:
        model_name: Registry key / model class name.
        model_cls: Python model class to compile.
        specs: Optional prebuilt column specs — avoids a redundant
            ``build_column_specs`` pass when the caller just built it.

    Returns:
        The compiled SchemaIR envelope for ``model_cls``.
    """
    if specs is None:
        specs = build_column_specs(model_cls)
    # Derive the PK fact (and run the at-most-one guard) before compile +
    # persist: a multi-PK model raises here, at class definition time, and
    # never persists an envelope or publishes specs.
    pk_field = primary_key_field_name(model_name, specs)
    column_names = frozenset(specs)
    uniques = normalized_composite_uniques(model_cls, column_names)
    indexes = drop_overlap_with_uniques(
        normalized_composite_indexes(model_cls, column_names), uniques, model_name
    )
    envelope = _compile_and_persist_model_envelope(
        model_name,
        tuple(specs.values()),
        table_name=getattr(model_cls, "__ferro_table__", None),
        composite_uniques=uniques,
        composite_indexes=indexes,
    )
    # Publish specs onto the class only after compile + persist succeed, so a
    # composite-validation or persist failure leaves the prior specs in place
    # (never fresh specs with no matching persisted envelope). This is the
    # "``__ferro_columns__`` can never go stale relative to the envelope"
    # invariant, made atomic.
    model_cls.__ferro_columns__ = specs
    # Same choke point caches the PK fact: every ``__ferro_columns__`` refresh
    # re-derives ``__ferro_pk__``, so it can never go stale relative to specs.
    model_cls.__ferro_pk__ = pk_field
    # Same choke point compiles relation-traversal facts (#268): every
    # ``__ferro_columns__`` refresh (provisional class-body registration and
    # the resolved second pass) refreshes ``__ferro_relation_specs__`` too, so
    # the latter is never stale relative to the former.
    model_cls.__ferro_relation_specs__ = build_relation_specs(model_cls, specs)
    # And the reverse-relation facts (#314, ADR-0007): the same refresh keeps
    # ``__ferro_reverse_specs__`` in step, so existence tests resolve against
    # descriptors the moment relationship resolution recompiles this model.
    model_cls.__ferro_reverse_specs__ = build_reverse_specs(model_cls)
    return envelope


def _model_payload_from_envelope(name: str) -> dict[str, Any]:
    """Return one model's SchemaIR payload object from the envelope cache.

    The returned dict is a live reference into the Registry's envelope cache —
    the assembled modelset shares these payload objects. Treat them as
    read-only; in-place mutation corrupts the per-model cache and survives
    clean-path re-assembles indefinitely (#245).
    """
    envelope = REGISTRY.envelope(name)
    if envelope is None:
        raise RuntimeError(
            f"Missing SchemaIR envelope for '{name}'. "
            "Compile the model (or run relationship resolution) before assembling "
            "the modelset."
        )
    return envelope["payload"]["models"][0]


def _assemble_modelset_envelope() -> dict[str, Any]:
    """Stitch per-model envelopes into one sorted modelset envelope."""
    models: list[dict[str, Any]] = []
    for model_name in sorted(REGISTRY.models()):
        models.append(_model_payload_from_envelope(model_name))

    for table_name, bundle in sorted(
        REGISTRY.join_tables().items(), key=lambda item: item[0]
    ):
        if not isinstance(bundle, dict):
            continue
        models.append(_model_payload_from_envelope(table_name))

    envelope = {
        "ir_kind": "schema",
        "ir_version": _IR_VERSION,
        "payload": {
            "dialect_agnostic": True,
            "models": models,
        },
    }

    REGISTRY.set_modelset(envelope)
    return envelope


def _register_join_table_bundle(table_name: str, bundle: dict[str, Any]) -> None:
    register_model_with_ir(
        table_name,
        bundle["columns"],
        table_name,
        composite_uniques=bundle["composite_uniques"],
        composite_indexes=bundle["composite_indexes"],
    )


def _recompile_missing_registry_envelopes() -> None:
    for model_name in REGISTRY.missing_envelope_names():
        model_cls = REGISTRY.models().get(model_name)
        if model_cls is not None:
            compile_model_schema_ir(model_name, model_cls)
            continue
        bundle = REGISTRY.join_tables().get(model_name)
        if isinstance(bundle, dict):
            _register_join_table_bundle(model_name, bundle)


def _recompile_all_registered_envelopes() -> None:
    """Recompile every registered model and join-table envelope.

    Used when ``compile_registry_schema_ir()`` is invoked on a dirty registry
    without going through ``resolve_relationships()`` — direct callers (tests,
    Alembic-adjacent tooling) historically relied on compile-not-assemble here.
    The connect/reconnect clean path never hits this (#245).
    """
    for model_name, model_cls in sorted(REGISTRY.models().items()):
        compile_model_schema_ir(model_name, model_cls)
    for table_name, bundle in sorted(
        REGISTRY.join_tables().items(), key=lambda item: item[0]
    ):
        if isinstance(bundle, dict):
            _register_join_table_bundle(table_name, bundle)


def compile_registry_schema_ir() -> dict[str, Any]:
    """Assemble and persist a deterministic SchemaIR envelope for all models.

    Stitches already-compiled per-model envelopes from the Registry's envelope
    cache for every registered model and join table.
    On a dirty registry, recompiles envelopes first so direct callers that
    bypass ``resolve_relationships()`` still observe current registry state.
    After ``ensure_resolved_modelset()`` on the clean path, this is
    assemble-only (#245).

    Returns:
        The assembled model-set SchemaIR envelope, sorted by model name.
    """
    if REGISTRY.is_dirty():
        _recompile_all_registered_envelopes()
    else:
        _recompile_missing_registry_envelopes()
    return _assemble_modelset_envelope()


def schema_ir_fingerprint(ir_envelope: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 fingerprint for a SchemaIR envelope."""
    return ir_fingerprint(ir_envelope)
