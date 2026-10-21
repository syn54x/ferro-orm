"""The registry: single owner of Ferro's Python-side registration state.

One `Registry` instance (`REGISTRY` — process-global by nature:
the metaclass writes to it at import time) owns the model registry, per-model
SchemaIR envelope + fingerprint caches, join-table bundles, pending
relations, the modelset artifact, and the provisional/resolved generation
counters (see "Provisional registration" and "Resolved registration" in
`GLOSSARY.md`).

The stores are private; every reader and writer goes through the interface,
so their agreement invariants have exactly one home:

- A stored fingerprint is always a pure function of the envelope it
  accompanies (`Registry.persist_envelope` / `Registry.set_modelset`
  compute it — no caller can persist a mismatched pair).
- Evicting join tables evicts their envelopes with them
  (`Registry.clear_join_tables` — previously a hand-written loop in
  ``clear_registry`` guarded by a warning comment).
- A test reset or snapshot/restore covers every store and counter in one
  call (`Registry.reset_for_test`, `Registry.snapshot`).
- A modelset that must never mix with today's (a data step's historical
  models) is built against an empty registry (`Registry.capture`) and
  installed for a block (`Registry.swap`), which restores today's on
  exit, error and cancellation.

Ordering caveat (unchanged from the pre-Registry design, closed by a future
atomic register→persist step): defining model classes concurrently with
``connect``/``create_tables``/``migrate`` is unsupported — a registry entry
becomes visible on `register` before its envelope is persisted, so an
unlocked reader can observe a transient missing-envelope state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol


class FerroModelClass(Protocol):
    """Structural type for a registrable Ferro model class.

    The registry keys by ``__ferro_identity__`` (what `Registry.register`
    derives its key from) and resolves bare references against ``__name__`` /
    ``__qualname__``. Typed as a ``Protocol`` rather than ``type[Model]`` so
    ``registry`` — which ``models`` and ``metaclass`` import — does not import
    back into them (circular import); a non-Ferro class handed to
    `Registry.register` is then a type error at the call site instead of
    a raw ``AttributeError`` at runtime.
    """

    __ferro_identity__: str
    __name__: str
    __qualname__: str


_UNSET = object()


def canonical_ir_json(value: dict[str, Any]) -> str:
    """Serialize a schema artifact as JSON with a deterministic key order.

    Args:
        value: The schema artifact.

    Returns:
        Compact JSON with sorted keys, ASCII only.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def ir_fingerprint(value: dict[str, Any]) -> str:
    """Return the canonical SHA-256 fingerprint of a schema artifact.

    The single source of the fingerprint function, so a stored fingerprint
    is always a pure function of the envelope it accompanies: the
    fingerprint gate reads these, and a mismatched pair would serve a stale
    schema silently.

    Args:
        value: The schema artifact.

    Returns:
        The hex SHA-256 of its canonical JSON.
    """
    return hashlib.sha256(canonical_ir_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegistrySnapshot:
    """An opaque copy of every `Registry` store and counter, for test fixtures."""

    models: dict[str, FerroModelClass]
    """The registered models, keyed by canonical identity."""
    envelopes: dict[str, dict[str, Any]]
    """The compiled schema envelopes, keyed by model identity or join-table name."""
    fingerprints: dict[str, str]
    """The fingerprint of each envelope, under the same keys."""
    join_tables: dict[str, Any]
    """The join-table bundles, keyed by table name."""
    pending_relations: list[Any] = field(repr=False)
    """The relation declarations waiting for resolution."""
    modelset: dict[str, Any] | None
    """The assembled modelset envelope, or `None` if never built."""
    modelset_fingerprint: str | None
    """The modelset's fingerprint, or `None` if never built."""
    registration_generation: int
    """The registration generation counter."""
    resolved_generation: int
    """The generation last cleared by relationship resolution."""


class SwappableModelset(Protocol):
    """What `Registry.swap` installs: a data step's historical models."""

    @property
    def registry_state(self) -> RegistrySnapshot:
        """The registry state holding exactly the modelset's classes."""
        ...

    def unreachable(self, cls: FerroModelClass) -> str:
        """Return why today's `cls` cannot be queried while the modelset is installed.

        Args:
            cls: A model class of today's codebase.

        Returns:
            The error text its entry points raise.
        """
        ...


class SwappedOutModelError(RuntimeError):
    """A model class from the codebase was queried while another modelset was installed.

    Inside a data step, query `ctx.models.<Name>` instead.
    """


class _SwappedOut:
    """Stands in for one entry point of a swapped-out model class; any access
    (``Author.where``, ``author.save``) raises with the modelset's text."""

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        raise SwappedOutModelError(self._text)


def _entry_points() -> list[str]:
    """Every public query and persistence entry point a model class has: the
    callables ``ferro.models.Model`` itself defines."""
    from .models import Model

    return [
        name
        for name, value in vars(Model).items()
        if not name.startswith("_")
        and callable(getattr(value, "__func__", value))
        and name != "model_config"
    ]


def _guard(cls: FerroModelClass, text: str) -> list[tuple[FerroModelClass, str, Any]]:
    """Shadow every entry point of ``cls`` with a `_SwappedOut`;
    return what to put back."""
    placed: list[tuple[FerroModelClass, str, Any]] = []
    for name in _entry_points():
        placed.append((cls, name, cls.__dict__.get(name, _UNSET)))
        setattr(cls, name, _SwappedOut(text))
    return placed


def _install_in_rust(state: RegistrySnapshot) -> None:
    """Install ``state``'s assembled modelset in the Rust runtime (through
    the fingerprint gate), or clear it when the state has none."""
    from . import _core

    if state.modelset is None or state.modelset_fingerprint is None:
        _core.clear_registry()
        return
    _core._install_registration(json.dumps(state.modelset), state.modelset_fingerprint)


class Registry:
    """The single owner of the registration stores; see the module docstring."""

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
        derived from ``cls`` here rather than caller-supplied: the registry
        key is a pure function of the model, so every emitter (the Rust
        runtime registry, the raw operations) keys the same model by the same
        name. Idempotent by construction.

        Args:
            cls: The model class.
        """
        self._models[cls.__ferro_identity__] = cls
        self._registration_generation += 1

    def deregister(self, name: str) -> None:
        """Evict one model from every per-model store (registry + envelopes).

        Takes a ``name`` (the canonical identity) rather than a class, unlike
        `register` — teardown paths iterate registry keys and hold the
        identity string, not the class object. Idempotent.

        Args:
            name: The model's canonical identity.
        """
        removed = name in self._models
        self._models.pop(name, None)
        self.evict_envelope(name)
        if removed:
            self._registration_generation += 1

    def models(self) -> dict[str, FerroModelClass]:
        """Return the registered models, keyed by canonical identity.

        A read view: callers iterate, look up, and pass it on (shadow
        foreign-key reconciliation, say) and never mutate it. Mutation goes
        through `register` and `deregister`, which own the generation
        counter bump.

        Returns:
            The live mapping of identity to model class.
        """
        return self._models

    def resolve_reference(self, ref: str, *, default: Any = _UNSET) -> Any:
        """Resolve a model reference to a registered model class.

        A qualified reference is a direct hit; a bare name matching exactly
        one registered model resolves to it.

        Args:
            ref: A qualified identity (`module.QualName`) or a bare class
                name.
            default: What to return when nothing matches. When not given,
                no match raises.

        Returns:
            The model class, or `default` when nothing matches.

        Raises:
            RuntimeError: A bare name matches several models (always, with
                the qualified candidates listed), or nothing matches and no
                `default` was given.
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
        """Store one model's (or join table's) compiled schema envelope.

        The fingerprint is computed here from the envelope, a pure function
        of it, so no caller can persist a stale envelope and fingerprint
        pair.

        Args:
            name: The key the schema compiler uses: a model's
                `__ferro_identity__` or a join table's name.
            envelope: The compiled envelope.
        """
        self._envelopes[name] = envelope
        self._fingerprints[name] = ir_fingerprint(envelope)

    def evict_envelope(self, name: str) -> None:
        """Evict one entry from the envelope and fingerprint caches only.

        Args:
            name: The model identity or join-table name.
        """
        self._envelopes.pop(name, None)
        self._fingerprints.pop(name, None)

    def envelope(self, name: str) -> dict[str, Any] | None:
        """Return one compiled envelope, as a live reference, not a copy.

        The modelset assembler reads it in place.

        Args:
            name: The model identity or join-table name.

        Returns:
            The envelope, or `None` when none is stored.
        """
        return self._envelopes.get(name)

    def fingerprint(self, name: str) -> str | None:
        """Return the stored fingerprint paired with `envelope`.

        Args:
            name: The model identity or join-table name.

        Returns:
            The fingerprint, or `None` when none is stored.
        """
        return self._fingerprints.get(name)

    def missing_envelope_names(self) -> list[str]:
        """Return the registered names whose envelope is absent.

        It is the store-agreement arithmetic: registry keys and dict-bundle
        join tables, minus envelope keys. A non-empty result means a cold
        rehydrate must recompile first.

        Returns:
            The model identities and join-table names with no envelope.
        """
        missing = [name for name in self._models if name not in self._envelopes]
        missing.extend(
            table_name
            for table_name, bundle in self._join_tables.items()
            if isinstance(bundle, dict) and table_name not in self._envelopes
        )
        return missing

    # -- join tables ---------------------------------------------------------

    def add_join_table(self, name: str, bundle: Any) -> None:
        """Record one generated join table's bundle.

        Args:
            name: The join table's name.
            bundle: What the schema compiler needs to build its envelope.
        """
        self._join_tables[name] = bundle

    def join_tables(self) -> dict[str, Any]:
        """Return the join-table bundles, keyed by table name, as a read view.

        Returns:
            The live mapping of table name to bundle.
        """
        return self._join_tables

    def clear_join_tables(self) -> None:
        """Drop every join-table bundle AND its envelope cache entry.

        Join tables never live in the model registry, so this touches nothing
        a declared model needs to cold-rehydrate; leaving their envelopes
        behind would resurrect stale join-table DDL on the next assemble.
        """
        for name in list(self._join_tables):
            self.evict_envelope(name)
        self._join_tables.clear()

    # -- pending relations ---------------------------------------------------

    def defer_relation(self, identity: str, field_name: str, metadata: Any) -> None:
        """Queue one relation declaration for resolution; the metaclass calls it.

        Args:
            identity: The declaring model's canonical identity.
            field_name: The relation field's name.
            metadata: The relation's declaration (`ForeignKey`,
                `ManyToManyRelation` or `"BackRef"`).
        """
        self._pending_relations.append((identity, field_name, metadata))

    def drain_pending_relations(self) -> list[Any]:
        """Return the pending relation declarations and clear the queue.

        Relationship resolution calls it.

        Returns:
            The queued `(identity, field_name, metadata)` entries, in order.
        """
        drained = list(self._pending_relations)
        self._pending_relations.clear()
        return drained

    # -- modelset artifact -----------------------------------------------------

    def set_modelset(self, envelope: dict[str, Any]) -> None:
        """Cache the assembled modelset envelope and compute its fingerprint.

        Args:
            envelope: The assembled modelset.
        """
        self._modelset = envelope
        self._modelset_fingerprint = ir_fingerprint(envelope)

    def modelset(self) -> tuple[dict[str, Any], str] | None:
        """Return the cached modelset and its fingerprint.

        Returns:
            The `(envelope, fingerprint)` pair, or `None` if never built.
        """
        if self._modelset is None or self._modelset_fingerprint is None:
            return None
        return self._modelset, self._modelset_fingerprint

    def clear_modelset(self) -> None:
        """Drop the cached modelset, so the next assemble rebuilds it.

        Per-model envelopes are untouched.
        """
        self._modelset = None
        self._modelset_fingerprint = None

    # -- generations -----------------------------------------------------------

    def registration_generation(self) -> int:
        """Return the registration generation counter, for tests and diagnostics.

        Returns:
            The counter; each `register` and `deregister` bumps it.
        """
        return self._registration_generation

    def resolved_generation(self) -> int:
        """Return the generation last cleared by relationship resolution.

        Returns:
            The registration generation at the last resolution.
        """
        return self._resolved_generation

    def mark_resolved(self) -> None:
        """Record that the current registry generation has been resolved."""
        self._resolved_generation = self._registration_generation

    def is_dirty(self) -> bool:
        """Return whether the registry needs resolving again.

        Returns:
            `True` when a model registered or deregistered since the last
            resolution, or relations are pending.
        """
        return self._registration_generation != self._resolved_generation or bool(
            self._pending_relations
        )

    # -- scoped modelsets -----------------------------------------------------------

    def capture[T](self, define: Callable[[], T]) -> tuple[T, RegistrySnapshot]:
        """Run `define` against an empty registry and capture what it registers.

        This is how a modelset that must never mix with today's is built:
        the classes `define` creates register, through the metaclass, into
        the empty registry. This registry's own state is restored whatever
        happens.

        Args:
            define: A callable that declares the modelset's classes.

        Returns:
            What `define` returned, and a `RegistrySnapshot` holding exactly
                the classes it registered, ready for `swap`.
        """
        today = self.snapshot()
        try:
            self.reset_for_test()
            result = define()
            return result, self.snapshot()
        finally:
            self.restore(today)

    @contextmanager
    def swap(self, modelset: SwappableModelset) -> Iterator[None]:
        """Install `modelset` as the registry for the block, restoring today's after.

        It is installed in Python and in the Rust runtime, and today's comes
        back on exit, error and cancellation.

        While it is installed, every query and persistence entry point of
        today's model classes (``Author.where``, ``author.save``, ...) raises
        `SwappedOutModelError` with ``modelset.unreachable(cls)``, so
        a data step can never query today's table shape by mistake. The Rust
        side is installed through the fingerprint gate, once on entry and
        once on exit.

        Args:
            modelset: The modelset to install, a data step's historical
                models.

        Yields:
            Nothing; the modelset is installed while the block runs.
        """
        today = self.snapshot()
        guarded: list[tuple[FerroModelClass, str, Any]] = []
        try:
            for cls in today.models.values():
                guarded.extend(_guard(cls, modelset.unreachable(cls)))
            self.restore(modelset.registry_state)
            _install_in_rust(modelset.registry_state)
            yield
        finally:
            self.restore(today)
            for cls, name, previous in reversed(guarded):
                if previous is _UNSET:
                    delattr(cls, name)
                else:
                    setattr(cls, name, previous)
            _install_in_rust(today)

    # -- test surface ------------------------------------------------------------

    def snapshot(self) -> RegistrySnapshot:
        """Copy every store and counter, for `restore` to put back.

        Returns:
            The copy.
        """
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
        """Replace every store and counter with the snapshot's contents.

        Args:
            snap: A copy from `snapshot` or `capture`.
        """
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
"""The process-wide registry the metaclass registers every model class in."""
