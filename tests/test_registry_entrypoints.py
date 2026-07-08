"""#243 prefactor: centralized register/deregister registry entrypoints.

Every per-model registry/envelope write and eviction goes through the single
``register_model`` / ``persist_model_envelope`` / ``deregister_model``
entrypoints in :mod:`ferro.state`; no ``src/`` code mutates
``_MODEL_REGISTRY_PY`` / ``_SCHEMA_IR_BY_MODEL`` (or their fingerprint maps)
directly. ``clear_registry()`` additionally evicts join-table envelopes from
the per-model envelope cache, preserving the #153 stale-join-table guard.

These are the "make the change easy" seams that a later slice (#242 generation
counter, bulk install) hooks; this slice is a no-behavior-change prefactor.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Annotated, ClassVar

from ferro import (
    BackRef,
    FerroField,
    ManyToMany,
    Model,
    Relation,
    clear_registry,
)


def test_register_model_is_the_single_registry_write(clean_registry):
    from ferro import state as ferro_state

    class Widget(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    identity = Widget.__ferro_identity__
    # Class-body registration flowed through the entrypoint into the registry
    # (keyed by the canonical identity) and persisted the envelope + fingerprint.
    assert ferro_state._MODEL_REGISTRY_PY[identity] is Widget
    assert identity in ferro_state._SCHEMA_IR_BY_MODEL
    assert identity in ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL


def test_register_model_keys_by_canonical_identity(clean_registry):
    from ferro import state as ferro_state
    from ferro.state import register_model

    class Marker(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    ferro_state._MODEL_REGISTRY_PY.clear()
    # #249: the key is derived from the model's canonical identity, never
    # caller-supplied. A bare ``__name__`` must never become a registry key.
    register_model(Marker)

    assert ferro_state._MODEL_REGISTRY_PY[Marker.__ferro_identity__] is Marker
    assert Marker.__name__ not in ferro_state._MODEL_REGISTRY_PY


def test_persist_model_envelope_fingerprint_matches_envelope(clean_registry):
    from ferro import state as ferro_state

    envelope = {"ir_kind": "schema", "ir_version": 1, "payload": {"models": []}}
    ferro_state.persist_model_envelope("x.Thing", envelope)

    assert ferro_state._SCHEMA_IR_BY_MODEL["x.Thing"] is envelope
    assert ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL[
        "x.Thing"
    ] == ferro_state.ir_fingerprint(envelope)


def test_deregister_model_evicts_registry_and_envelope(clean_registry):
    from ferro import state as ferro_state
    from ferro.state import deregister_model

    class Gadget(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    identity = Gadget.__ferro_identity__
    assert identity in ferro_state._MODEL_REGISTRY_PY
    assert identity in ferro_state._SCHEMA_IR_BY_MODEL
    assert identity in ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL

    deregister_model(identity)

    assert identity not in ferro_state._MODEL_REGISTRY_PY
    assert identity not in ferro_state._SCHEMA_IR_BY_MODEL
    assert identity not in ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL


def test_evict_model_envelope_leaves_model_registry(clean_registry):
    from ferro import state as ferro_state
    from ferro.state import evict_model_envelope

    class Doohickey(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    identity = Doohickey.__ferro_identity__
    evict_model_envelope(identity)

    # Envelope-only: the model registry entry survives (cold-rehydration path).
    assert identity in ferro_state._MODEL_REGISTRY_PY
    assert identity not in ferro_state._SCHEMA_IR_BY_MODEL
    assert identity not in ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL


def test_deregister_model_is_idempotent(clean_registry):
    from ferro.state import deregister_model

    # Evicting an unknown identity is a no-op, not a KeyError.
    deregister_model("does.not.Exist")


def test_clear_registry_evicts_join_table_envelopes(clean_registry):
    """#153 guard: a stale join-table envelope must not survive clear_registry.

    A join table left behind in the per-model envelope cache would let the
    assembled modelset resurrect foreign keys to tables that no longer exist —
    tolerated by SQLite, rejected by Postgres.
    """
    from ferro import state as ferro_state
    from ferro.relations import resolve_relationships

    class Page(Model):
        __ferro_table__: ClassVar[str] = "reg_pages"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        tags: Relation[list["PageTag"]] = ManyToMany(related_name="pages")

    class PageTag(Model):
        __ferro_table__: ClassVar[str] = "reg_page_tags"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        pages: Relation[list[Page]] = BackRef()

    resolve_relationships()

    join_table = "reg_pages_tags"
    assert join_table in ferro_state._JOIN_TABLE_REGISTRY
    assert join_table in ferro_state._SCHEMA_IR_BY_MODEL
    assert join_table in ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL

    clear_registry()

    assert join_table not in ferro_state._JOIN_TABLE_REGISTRY
    assert join_table not in ferro_state._SCHEMA_IR_BY_MODEL
    assert join_table not in ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL


def test_clear_registry_keeps_model_registry(clean_registry):
    """clear_registry() promises declared models survive (cold rehydration)."""
    from ferro import state as ferro_state

    class Survivor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    identity = Survivor.__ferro_identity__
    clear_registry()
    assert ferro_state._MODEL_REGISTRY_PY[identity] is Survivor


# ---------------------------------------------------------------------------
# Single-writer guard: an AST walk (comments/strings/formatting-agnostic) is
# the source of truth for "no direct per-model store write escapes the
# entrypoints". It catches subscript assignment (incl. nested keys and
# multi-line statements), augmented assignment (`|=`), `del`, and the mutating
# dict methods (`pop`, `popitem`, `clear`, `setdefault`, `update`).
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "ferro"
_STORE_NAMES = {
    "_MODEL_REGISTRY_PY",
    "_SCHEMA_IR_BY_MODEL",
    "_SCHEMA_IR_FINGERPRINT_BY_MODEL",
}
_MUTATING_METHODS = {"pop", "popitem", "clear", "setdefault", "update"}
# state.py is where the entrypoints live — the one file allowed to mutate the
# stores directly. Matched by path relative to _SRC_ROOT (not basename), so a
# nested ``.../state.py`` would still be scanned.
_ALLOWED_RELPATHS = {Path("state.py")}


def _refers_to_store(node: ast.AST) -> bool:
    """True if ``node`` (a subscript/method container) resolves to a store.

    Peels nested subscripts (``store[a][b]``) down to the container, then matches
    a bare imported name (``_MODEL_REGISTRY_PY``) or an attribute access
    (``ferro_state._MODEL_REGISTRY_PY``).
    """
    while isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id in _STORE_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _STORE_NAMES
    return False


def _is_store_ref(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _STORE_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _STORE_NAMES
    return False


def _store_write_linenos(source: str) -> list[int]:
    """Line numbers of every direct per-model store mutation in ``source``."""
    tree = ast.parse(source)
    hits: list[int] = []
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]

        for target in targets:
            if isinstance(target, ast.Subscript) and _refers_to_store(target.value):
                hits.append(node.lineno)
            elif _is_store_ref(target):  # whole-store rebind / `store |= ...`
                hits.append(node.lineno)

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _MUTATING_METHODS and _refers_to_store(
                node.func.value
            ):
                hits.append(node.lineno)
    return sorted(set(hits))


def _iter_src_files():
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path.relative_to(_SRC_ROOT) in _ALLOWED_RELPATHS:
            continue
        yield path


def test_no_direct_store_writes_outside_state():
    """The register/deregister entrypoints own every per-model store write."""
    scanned = 0
    offenders: list[str] = []
    for path in _iter_src_files():
        scanned += 1
        source = path.read_text(encoding="utf-8")
        for lineno in _store_write_linenos(source):
            rel = path.relative_to(_SRC_ROOT)
            offenders.append(f"{rel}:{lineno}")
    # Guard against a silently-empty scan (e.g. _SRC_ROOT drifting).
    assert scanned >= 10, f"expected to scan the ferro package, only saw {scanned} files"
    assert not offenders, (
        "Direct per-model registry/envelope writes must route through "
        "ferro.state.register_model / persist_model_envelope / "
        "evict_model_envelope / deregister_model:\n" + "\n".join(offenders)
    )


def test_store_write_detector_flags_known_idioms():
    """Positive control: the detector catches every idiom it claims to.

    Pins that the guard is not vacuous — these are exactly the escape hatches a
    #242 implementer might reach for.
    """
    sample = "\n".join(
        [
            "_MODEL_REGISTRY_PY[k] = v",  # subscript assign
            "_SCHEMA_IR_BY_MODEL[names[0]] = v",  # nested-subscript key
            "_SCHEMA_IR_BY_MODEL[name]['payload'] = v",  # in-place mutation
            "_MODEL_REGISTRY_PY.setdefault(k, v)",
            "_MODEL_REGISTRY_PY.update(other)",
            "_MODEL_REGISTRY_PY.pop(k, None)",
            "_SCHEMA_IR_BY_MODEL.popitem()",
            "_SCHEMA_IR_FINGERPRINT_BY_MODEL.clear()",
            "del _MODEL_REGISTRY_PY[k]",
            "_MODEL_REGISTRY_PY |= other",
            "ferro_state._MODEL_REGISTRY_PY[k] = v",  # attribute-qualified access
            "ferro_state._SCHEMA_IR_BY_MODEL.pop(k, None)",
            "_MODEL_REGISTRY_PY[\n    k\n] = v",  # multi-line statement
        ]
    )
    # Each statement is one logical write; the multi-line one spans 3 source
    # lines but is a single hit.
    assert len(_store_write_linenos(sample)) == 13

    clean = "\n".join(
        [
            "value = _MODEL_REGISTRY_PY[k]",  # read
            "snapshot = list(_MODEL_REGISTRY_PY.items())",  # iteration
            "found = _SCHEMA_IR_BY_MODEL.get(k)",  # get
            "present = k in _MODEL_REGISTRY_PY",  # membership
            "source_model = _MODEL_REGISTRY_PY[name]",  # RHS subscript read
        ]
    )
    assert _store_write_linenos(clean) == []
