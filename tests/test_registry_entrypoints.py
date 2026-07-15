"""#243 prefactor: centralized register/deregister registry entrypoints.

Every per-model registry/envelope write and eviction goes through the single
``REGISTRY.register`` / ``REGISTRY.persist_envelope`` / ``REGISTRY.deregister``
entrypoints on :data:`ferro.registry.REGISTRY`; no ``src/`` code mutates the
Registry's private stores (``_models`` / ``_envelopes`` / ``_fingerprints``)
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
    from ferro.registry import REGISTRY

    class Widget(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    identity = Widget.__ferro_identity__
    # Class-body registration flowed through the entrypoint into the registry
    # (keyed by the canonical identity) and persisted the envelope + fingerprint.
    assert REGISTRY.models()[identity] is Widget
    assert REGISTRY.envelope(identity) is not None
    assert REGISTRY.fingerprint(identity) is not None


def test_register_model_keys_by_canonical_identity(clean_registry):
    from ferro.registry import REGISTRY

    class Marker(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    REGISTRY.deregister(Marker.__ferro_identity__)
    # #249: the key is derived from the model's canonical identity, never
    # caller-supplied. A bare ``__name__`` must never become a registry key.
    REGISTRY.register(Marker)

    assert REGISTRY.models()[Marker.__ferro_identity__] is Marker
    assert Marker.__name__ not in REGISTRY.models()


def test_persist_model_envelope_fingerprint_matches_envelope(clean_registry):
    from ferro.registry import REGISTRY, ir_fingerprint

    envelope = {"ir_kind": "schema", "ir_version": 1, "payload": {"models": []}}
    REGISTRY.persist_envelope("x.Thing", envelope)

    assert REGISTRY.envelope("x.Thing") is envelope
    assert REGISTRY.fingerprint("x.Thing") == ir_fingerprint(envelope)


def test_deregister_model_evicts_registry_and_envelope(clean_registry):
    from ferro.registry import REGISTRY

    class Gadget(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    identity = Gadget.__ferro_identity__
    assert identity in REGISTRY.models()
    assert REGISTRY.envelope(identity) is not None
    assert REGISTRY.fingerprint(identity) is not None

    REGISTRY.deregister(identity)

    assert identity not in REGISTRY.models()
    assert REGISTRY.envelope(identity) is None
    assert REGISTRY.fingerprint(identity) is None


def test_evict_model_envelope_leaves_model_registry(clean_registry):
    from ferro.registry import REGISTRY

    class Doohickey(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    identity = Doohickey.__ferro_identity__
    REGISTRY.evict_envelope(identity)

    # Envelope-only: the model registry entry survives (cold-rehydration path).
    assert identity in REGISTRY.models()
    assert REGISTRY.envelope(identity) is None
    assert REGISTRY.fingerprint(identity) is None


def test_deregister_model_is_idempotent(clean_registry):
    from ferro.registry import REGISTRY

    # Evicting an unknown identity is a no-op, not a KeyError.
    REGISTRY.deregister("does.not.Exist")


def test_clear_registry_evicts_join_table_envelopes(clean_registry):
    """#153 guard: a stale join-table envelope must not survive clear_registry.

    A join table left behind in the per-model envelope cache would let the
    assembled modelset resurrect foreign keys to tables that no longer exist —
    tolerated by SQLite, rejected by Postgres.
    """
    from ferro.registry import REGISTRY
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
    assert join_table in REGISTRY.join_tables()
    assert REGISTRY.envelope(join_table) is not None
    assert REGISTRY.fingerprint(join_table) is not None

    clear_registry()

    assert join_table not in REGISTRY.join_tables()
    assert REGISTRY.envelope(join_table) is None
    assert REGISTRY.fingerprint(join_table) is None


def test_clear_registry_keeps_model_registry(clean_registry):
    """clear_registry() promises declared models survive (cold rehydration)."""
    from ferro.registry import REGISTRY

    class Survivor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    identity = Survivor.__ferro_identity__
    clear_registry()
    assert REGISTRY.models()[identity] is Survivor


# ---------------------------------------------------------------------------
# Single-writer guard: an AST walk (comments/strings/formatting-agnostic) is
# the source of truth for "no direct per-model store write escapes the
# entrypoints". It catches subscript assignment (incl. nested keys and
# multi-line statements), augmented assignment (`|=`), `del`, and the mutating
# dict methods (`pop`, `popitem`, `clear`, `setdefault`, `update`).
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "ferro"
_STORE_NAMES = {
    "_models",
    "_envelopes",
    "_fingerprints",
}
_MUTATING_METHODS = {"pop", "popitem", "clear", "setdefault", "update"}
# registry.py is where the Registry entrypoints live — the one file allowed to
# mutate the stores directly. Matched by path relative to _SRC_ROOT (not
# basename), so a nested ``.../registry.py`` would still be scanned.
_ALLOWED_RELPATHS = {Path("registry.py")}


def _refers_to_store(node: ast.AST) -> bool:
    """True if ``node`` (a subscript/method container) resolves to a store.

    Peels nested subscripts (``store[a][b]``) down to the container, then matches
    a bare name (``_models``) or an attribute access (``REGISTRY._models`` /
    ``self._models``).
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
        "REGISTRY.register / REGISTRY.persist_envelope / "
        "REGISTRY.evict_envelope / REGISTRY.deregister:\n" + "\n".join(offenders)
    )


def test_store_write_detector_flags_known_idioms():
    """Positive control: the detector catches every idiom it claims to.

    Pins that the guard is not vacuous — these are exactly the escape hatches a
    #242 implementer might reach for.
    """
    sample = "\n".join(
        [
            "_models[k] = v",  # subscript assign
            "_envelopes[names[0]] = v",  # nested-subscript key
            "_envelopes[name]['payload'] = v",  # in-place mutation
            "_models.setdefault(k, v)",
            "_models.update(other)",
            "_models.pop(k, None)",
            "_envelopes.popitem()",
            "_fingerprints.clear()",
            "del _models[k]",
            "_models |= other",
            "REGISTRY._models[k] = v",  # attribute-qualified access
            "REGISTRY._envelopes.pop(k, None)",
            "_models[\n    k\n] = v",  # multi-line statement
        ]
    )
    # Each statement is one logical write; the multi-line one spans 3 source
    # lines but is a single hit.
    assert len(_store_write_linenos(sample)) == 13

    clean = "\n".join(
        [
            "value = _models[k]",  # read
            "snapshot = list(_models.items())",  # iteration
            "found = _envelopes.get(k)",  # get
            "present = k in _models",  # membership
            "source_model = _models[name]",  # RHS subscript read
        ]
    )
    assert _store_write_linenos(clean) == []
