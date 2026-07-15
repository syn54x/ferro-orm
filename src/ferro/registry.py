"""The registry: single owner of Ferro's Python-side registration state.

One :class:`Registry` instance (:data:`REGISTRY` — process-global by nature:
the metaclass writes to it at import time) owns the model registry, per-model
SchemaIR envelope + fingerprint caches, join-table bundles, pending
relations, the modelset artifact, and the provisional/resolved generation
counters (CONTEXT.md "Provisional registration" / "Resolved registration").

The stores are private; every reader and writer goes through the interface,
so their agreement invariants have exactly one home:

- A stored fingerprint is always a pure function of the envelope it
  accompanies (:meth:`Registry.persist_envelope` / :meth:`Registry.set_modelset`
  compute it — no caller can persist a mismatched pair).
- Evicting join tables evicts their envelopes with them
  (:meth:`Registry.clear_join_tables` — previously a hand-written loop in
  ``clear_registry`` guarded by a warning comment).
- A test reset or snapshot/restore covers every store and counter in one
  call (:meth:`Registry.reset_for_test`, :meth:`Registry.snapshot`).

Ordering caveat (unchanged from the pre-Registry design, closed by a future
atomic register→persist step): defining model classes concurrently with
``connect``/``create_tables``/``migrate`` is unsupported — a registry entry
becomes visible on :meth:`register` before its envelope is persisted, so an
unlocked reader can observe a transient missing-envelope state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class FerroModelClass(Protocol):
    """Structural type for a registrable Ferro model class.

    The registry keys by ``__ferro_identity__`` (what :meth:`Registry.register`
    derives its key from) and resolves bare references against ``__name__`` /
    ``__qualname__``. Typed as a ``Protocol`` rather than ``type[Model]`` so
    ``registry`` — which ``models`` and ``metaclass`` import — does not import
    back into them (circular import); a non-Ferro class handed to
    :meth:`Registry.register` is then a type error at the call site instead of
    a raw ``AttributeError`` at runtime.
    """

    __ferro_identity__: str
    __name__: str
    __qualname__: str


_UNSET = object()


def canonical_ir_json(value: dict[str, Any]) -> str:
    """Serialize an IR artifact with deterministic key ordering."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def ir_fingerprint(value: dict[str, Any]) -> str:
    """Return the canonical SHA-256 fingerprint for an IR artifact.

    Single source of the fingerprint function so a stored fingerprint is
    always a pure function of the envelope it accompanies (the ADR
    fingerprint gate reads these; a mismatched pair would serve stale schema
    silently).
    """
    return hashlib.sha256(canonical_ir_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegistrySnapshot:
    """Opaque copy of every Registry store and counter (test fixtures)."""

    models: dict[str, FerroModelClass]
    envelopes: dict[str, dict[str, Any]]
    fingerprints: dict[str, str]
    join_tables: dict[str, Any]
    pending_relations: list[Any] = field(repr=False)
    modelset: dict[str, Any] | None
    modelset_fingerprint: str | None
    registration_generation: int
    resolved_generation: int


class Registry:
    """Single owner of the registration stores — see module docstring."""

    def __init__(self) -> None:
        self._models: dict[str, FerroModelClass] = {}
        self._envelopes: dict[str, dict[str, Any]] = {}
        self._fingerprints: dict[str, str] = {}
        self._join_tables: dict[str, Any] = {}
        self._pending_relations: list[Any] = []
        self._modelset: dict[str, Any] | None = None
        self._modelset_fingerprint: str | None = None
        # Monotonic generation counter bumped by register/deregister; matched
        # to resolved at the end of relationship resolution (#245).
        self._registration_generation = 0
        self._resolved_generation = 0

    # -- model registry ----------------------------------------------------

    def register(self, cls: FerroModelClass) -> None:
        """Record a model class (provisional registration).

        The key is the model's canonical identity (``__ferro_identity__``),
        derived from ``cls`` here rather than caller-supplied (#249): the
        registry key is a pure function of the model, so every emitter — the
        Rust runtime registry, the raw-FFI operations — keys the same model
        by the same name. Idempotent by construction.
        """
        self._models[cls.__ferro_identity__] = cls
        self._registration_generation += 1

    def deregister(self, name: str) -> None:
        """Evict one model from every per-model store (registry + envelopes).

        Takes a ``name`` (the canonical identity) rather than a class, unlike
        :meth:`register` — teardown paths iterate registry keys and hold the
        identity string, not the class object. Idempotent.
        """
        removed = name in self._models
        self._models.pop(name, None)
        self.evict_envelope(name)
        if removed:
            self._registration_generation += 1

    def models(self) -> dict[str, FerroModelClass]:
        """The registered models, keyed by canonical identity.

        A read view: callers iterate, look up, and pass it on (e.g. shadow-FK
        reconciliation) — they never mutate it. Mutation goes through
        :meth:`register` / :meth:`deregister`, which own the generation
        counter bump.
        """
        return self._models

    def resolve_reference(self, ref: str, *, default: Any = _UNSET) -> Any:
        """Resolve a model reference to a registered model class (FF-E E1).

        Accepts a qualified identity (``module.QualName``) or a bare class
        name. A qualified reference is a direct hit; a bare name matching
        exactly one registered model resolves to it; several matches raise
        with the qualified candidates listed; no match raises (or returns
        ``default`` when given). Ambiguity always raises.
        """
        model = self._models.get(ref)
        if model is not None:
            return model
        candidates = [cls for cls in self._models.values() if cls.__name__ == ref]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            listed = ", ".join(
                sorted(
                    getattr(c, "__ferro_identity__", c.__qualname__) for c in candidates
                )
            )
            raise RuntimeError(
                f"Model reference '{ref}' is ambiguous: {listed}. "
                "Use the qualified 'module.QualName' form to disambiguate."
            )
        if default is not _UNSET:
            return default
        raise RuntimeError(f"Model '{ref}' not found in registry")

    # -- per-model envelopes -----------------------------------------------

    def persist_envelope(self, name: str, envelope: dict[str, Any]) -> None:
        """Store one model's (or join table's) compiled SchemaIR envelope.

        The fingerprint is computed here from the envelope — a pure function
        of it, so no caller can persist a stale envelope/fingerprint pair.
        Keyed by the same ``name`` the IR compiler uses (a model's
        ``__ferro_identity__`` or a join-table name).
        """
        self._envelopes[name] = envelope
        self._fingerprints[name] = ir_fingerprint(envelope)

    def evict_envelope(self, name: str) -> None:
        """Evict one entry from the envelope + fingerprint caches only."""
        self._envelopes.pop(name, None)
        self._fingerprints.pop(name, None)

    def envelope(self, name: str) -> dict[str, Any] | None:
        """One compiled envelope, or None — a live reference, not a copy
        (the modelset assembler reads it in place)."""
        return self._envelopes.get(name)

    def fingerprint(self, name: str) -> str | None:
        """The stored fingerprint paired with :meth:`envelope`."""
        return self._fingerprints.get(name)

    def missing_envelope_names(self) -> list[str]:
        """Registered names whose envelope is absent — the store-agreement
        arithmetic (registry keys and dict-bundle join tables minus envelope
        keys). Non-empty means a cold-rehydrate must recompile first."""
        missing = [name for name in self._models if name not in self._envelopes]
        missing.extend(
            table_name
            for table_name, bundle in self._join_tables.items()
            if isinstance(bundle, dict) and table_name not in self._envelopes
        )
        return missing

    # -- join tables ---------------------------------------------------------

    def add_join_table(self, name: str, bundle: Any) -> None:
        """Record one auto-generated join table's bundle."""
        self._join_tables[name] = bundle

    def join_tables(self) -> dict[str, Any]:
        """The join-table bundles, keyed by table name (read view)."""
        return self._join_tables

    def clear_join_tables(self) -> None:
        """Drop every join-table bundle AND its envelope cache entry.

        Join tables never live in the model registry, so this touches nothing
        a declared model needs to cold-rehydrate; leaving their envelopes
        behind would resurrect stale join-table DDL on the next assemble
        (the #153 guard depends on these stores agreeing).
        """
        for name in list(self._join_tables):
            self.evict_envelope(name)
        self._join_tables.clear()

    # -- pending relations ---------------------------------------------------

    def defer_relation(self, identity: str, field_name: str, metadata: Any) -> None:
        """Queue one relationship declaration for resolution (metaclass)."""
        self._pending_relations.append((identity, field_name, metadata))

    def drain_pending_relations(self) -> list[Any]:
        """Return-and-clear the pending relation queue (resolution pass)."""
        drained = list(self._pending_relations)
        self._pending_relations.clear()
        return drained

    # -- modelset artifact -----------------------------------------------------

    def set_modelset(self, envelope: dict[str, Any]) -> None:
        """Cache the assembled modelset envelope; fingerprint computed here."""
        self._modelset = envelope
        self._modelset_fingerprint = ir_fingerprint(envelope)

    def modelset(self) -> tuple[dict[str, Any], str] | None:
        """The cached (envelope, fingerprint) pair, or None if never built."""
        if self._modelset is None or self._modelset_fingerprint is None:
            return None
        return self._modelset, self._modelset_fingerprint

    def clear_modelset(self) -> None:
        """Drop the cached modelset pair, forcing the next assemble to
        rebuild it (per-model envelopes are untouched)."""
        self._modelset = None
        self._modelset_fingerprint = None

    # -- generations -----------------------------------------------------------

    def registration_generation(self) -> int:
        """Current registration generation counter (test/diagnostic)."""
        return self._registration_generation

    def resolved_generation(self) -> int:
        """The generation last cleared by relationship resolution."""
        return self._resolved_generation

    def mark_resolved(self) -> None:
        """Record that the current registry generation has been resolved."""
        self._resolved_generation = self._registration_generation

    def is_dirty(self) -> bool:
        """True when the registry changed since last resolve or relations
        are pending."""
        return self._registration_generation != self._resolved_generation or bool(
            self._pending_relations
        )

    # -- test surface ------------------------------------------------------------

    def snapshot(self) -> RegistrySnapshot:
        """Copy every store and counter — pair with :meth:`restore`."""
        return RegistrySnapshot(
            models=dict(self._models),
            envelopes=dict(self._envelopes),
            fingerprints=dict(self._fingerprints),
            join_tables=dict(self._join_tables),
            pending_relations=list(self._pending_relations),
            modelset=self._modelset,
            modelset_fingerprint=self._modelset_fingerprint,
            registration_generation=self._registration_generation,
            resolved_generation=self._resolved_generation,
        )

    def restore(self, snap: RegistrySnapshot) -> None:
        """Replace every store and counter with the snapshot's contents."""
        self._models.clear()
        self._models.update(snap.models)
        self._envelopes.clear()
        self._envelopes.update(snap.envelopes)
        self._fingerprints.clear()
        self._fingerprints.update(snap.fingerprints)
        self._join_tables.clear()
        self._join_tables.update(snap.join_tables)
        self._pending_relations.clear()
        self._pending_relations.extend(snap.pending_relations)
        self._modelset = snap.modelset
        self._modelset_fingerprint = snap.modelset_fingerprint
        self._registration_generation = snap.registration_generation
        self._resolved_generation = snap.resolved_generation

    def reset_for_test(self) -> None:
        """Wipe every store and counter (``clean_registry`` fixture)."""
        self._models.clear()
        self._envelopes.clear()
        self._fingerprints.clear()
        self._join_tables.clear()
        self._pending_relations.clear()
        self._modelset = None
        self._modelset_fingerprint = None
        self._registration_generation = 0
        self._resolved_generation = 0


REGISTRY = Registry()
