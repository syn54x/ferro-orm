# Shadow FK types and forward-reference reconciliation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Derive shadow `{relation}_id` Pydantic types from the related model’s primary key type, keep a safe fallback when `ForeignKey.to` is unresolved, unwrap `Target | None` for FK metadata, and after `resolve_relationships()` upgrade shadows + `model_rebuild` + Rust schema registration so forward refs match concrete FK behavior (per `docs/superpowers/specs/2026-04-22-shadow-fk-types-design.md`).

**Architecture:** Shared pure helpers live in `src/ferro/_shadow_fk_types.py` (no import from `models` to avoid cycles). `ModelMetaclass` calls them when injecting shadows and when scanning `Annotated` FK/M2M targets. `resolve_relationships()` runs a reconciliation pass after binding `rel.to`, then `model_rebuild(force=True)` on touched models, then a single consolidated schema re-registration path for all registry models.

**Tech stack:** Python 3.13+, Pydantic v2 (`model_rebuild`), existing Ferro `ForeignKey` / `_MODEL_REGISTRY_PY` / `register_model_schema`.

---

## File map


| File                                | Role                                                                                                                                                                                                                        |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/ferro/_shadow_fk_types.py`     | **Create** — PK resolution, optional wrapping, fallback detection, annotation comparison, `reconcile_shadow_fk_types(registry: dict[str, type]) -> list[type]`.                                                             |
| `src/ferro/metaclass.py`            | **Modify** — FK/M2M scan: strip `                                                                                                                                                                                           |
| `src/ferro/relations/__init__.py`   | **Modify** — After the first `for model_name, field_name, rel in to_process:` loop, call reconciliation + rebuilds; extract or inline schema re-register loop so rebuilt models get correct `__ferro_schema_`_ / Rust JSON. |
| `tests/test_shadow_fk_types.py`     | **Create** — Unit tests for helpers; UUID + forward-ref integration-style tests (registry + `resolve_relationships`).                                                                                                       |
| `tests/test_metaclass_internals.py` | **Modify** — Expectations for `_inject_shadow_fields` when `fk.to` is a concrete model with known PK type.                                                                                                                  |
| `tests/test_relationship_engine.py` | **Modify** — Extend `test_forward_ref_resolution` (or add sibling test) to assert `Post.__annotations__["author_id"]` matches `Author` PK after `resolve_relationships()`.                                                  |


---

### Task 1: Helper module — types and PK resolution

**Files:**

- Create: `src/ferro/_shadow_fk_types.py`
- Test: `tests/test_shadow_fk_types.py` (added in Task 2 after red phase — or create minimal stub first; here implement module first per dependency order)

Implement the module below (single file). **Do not** import `Model` from `models`; use duck typing (`hasattr(cls, "ferro_fields")` and `model_fields`).

- **Step 1: Add `src/ferro/_shadow_fk_types.py`**

```python
"""Shadow foreign-key column typing helpers (issue #16, design spec 2026-04-22)."""

from __future__ import annotations

import types
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

# Matches metaclass fallback for unresolved FK targets
_FALLBACK_SHADOW_ANNOTATION = Union[int, str, None]


def is_concrete_ferro_model(obj: Any) -> bool:
    return isinstance(obj, type) and issubclass(obj, BaseModel) and hasattr(
        obj, "ferro_fields"
    )


def _scalar_part_of_annotation(ann: Any) -> Any:
    """Strip ``Annotated[T, ...]`` to ``T`` so shadow columns stay plain unions/scalars."""
    origin = get_origin(ann)
    if origin is Annotated:
        return get_args(ann)[0]
    return ann


def pk_python_type_for_model(target: type[Any]) -> Any | None:
    """Return the PK field's scalar annotation (inner ``T`` of ``Annotated[T, ...]``), or None."""
    ferro_fields = getattr(target, "ferro_fields", None)
    if not ferro_fields:
        return None
    pk_name = None
    for fname, fmeta in ferro_fields.items():
        if getattr(fmeta, "primary_key", False):
            pk_name = fname
            break
    if pk_name is None:
        return None
    mf = getattr(target, "model_fields", {}).get(pk_name)
    if mf is None:
        return None
    return _scalar_part_of_annotation(mf.annotation)


def shadow_annotation_for_pk(pk_ann: Any) -> Any:
    """Shadow *_id is optional at the ORM level (default None before assignment)."""
    if pk_ann is None:
        return _FALLBACK_SHADOW_ANNOTATION
    pk_ann = _scalar_part_of_annotation(pk_ann)
    origin = get_origin(pk_ann)
    args = get_args(pk_ann)
    if origin is Union or origin is types.UnionType:
        if type(None) in args:
            return pk_ann
    return pk_ann | None


def shadow_annotation_for_foreign_key(metadata: Any) -> Any:
    """Annotation for {name}_id at class creation time."""
    from .base import ForeignKey  # local import to avoid cycles at module import

    if not isinstance(metadata, ForeignKey):
        return _FALLBACK_SHADOW_ANNOTATION
    to = metadata.to
    if not is_concrete_ferro_model(to):
        return _FALLBACK_SHADOW_ANNOTATION
    pk_ann = pk_python_type_for_model(to)
    return shadow_annotation_for_pk(pk_ann)


def is_fallback_shadow_annotation(ann: Any) -> bool:
    """True if annotation is the legacy Union[int, str, None] shadow."""
    if ann is _FALLBACK_SHADOW_ANNOTATION:
        return True
    origin = get_origin(ann)
    if origin is not Union and origin is not types.UnionType:
        return False
    args = set(get_args(ann))
    return args == {int, str, type(None)}


def reconcile_shadow_fk_types(registry: dict[str, type[Any]]) -> list[type[Any]]:
    """
    After ForeignKey.to is concrete, upgrade shadow *_id annotations from fallback.

    Mutates cls.__annotations__ and calls model_rebuild(force=True) per changed class.

    Returns model classes that were rebuilt.
    """
    from .base import ForeignKey

    rebuilt: list[type[Any]] = []
    for model_name, model_cls in registry.items():
        if model_name == "Model":
            continue
        relations = getattr(model_cls, "ferro_relations", None)
        if not relations:
            continue
        changed = False
        for fname, meta in relations.items():
            if not isinstance(meta, ForeignKey):
                continue
            if not is_concrete_ferro_model(meta.to):
                continue
            pk_ann = pk_python_type_for_model(meta.to)
            if pk_ann is None:
                continue
            desired = shadow_annotation_for_pk(pk_ann)
            id_field = f"{fname}_id"
            if id_field not in getattr(model_cls, "model_fields", {}):
                continue
            current = model_cls.__annotations__.get(id_field)
            if current == desired:
                continue
            if current is not None and not is_fallback_shadow_annotation(current):
                # Explicit non-fallback annotation: do not override (custom user types)
                continue
            ann = model_cls.__dict__.get("__annotations__")
            if ann is None:
                model_cls.__annotations__ = {}
            else:
                model_cls.__annotations__ = dict(ann)
            model_cls.__annotations__[id_field] = desired
            changed = True
        if changed:
            model_cls.model_rebuild(force=True)
            rebuilt.append(model_cls)
    return rebuilt
```

- **Step 2: Commit**

```bash
git add src/ferro/_shadow_fk_types.py
git commit -m "feat: add shadow FK type helpers for PK-derived annotations"
```

---

### Task 2: Unit tests for `_shadow_fk_types`

**Files:**

- Create: `tests/test_shadow_fk_types.py`
- **Step 1: Write failing tests**

```python
"""Tests for ferro._shadow_fk_types."""

from typing import Annotated, Union
from uuid import UUID

import pytest

from uuid import uuid4

from ferro import FerroField, Field, ForeignKey, Model
from ferro._shadow_fk_types import (
    is_fallback_shadow_annotation,
    pk_python_type_for_model,
    shadow_annotation_for_foreign_key,
    shadow_annotation_for_pk,
)
from ferro.base import ForeignKey as ForeignKeyCls


def test_pk_python_type_for_model_uuid():
    class Parent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str

    assert pk_python_type_for_model(Parent) is UUID


def test_shadow_annotation_for_pk_wraps_required_uuid():
    ann = shadow_annotation_for_pk(UUID)
    assert ann == UUID | None


def test_is_fallback_shadow_annotation():
    assert is_fallback_shadow_annotation(Union[int, str, None])


def test_shadow_annotation_for_foreign_key_concrete():
    class Parent(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    fk = ForeignKeyCls(related_name="children")
    fk.to = Parent
    assert shadow_annotation_for_foreign_key(fk) == (int | None)


def test_shadow_annotation_for_foreign_key_unresolved_string():
    fk = ForeignKeyCls(related_name="children")
    fk.to = "Parent"
    ann = shadow_annotation_for_foreign_key(fk)
    assert is_fallback_shadow_annotation(ann)


@pytest.fixture
def _cleanup_registry():
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    yield
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()


def test_reconcile_upgrades_forward_ref_shadow(_cleanup_registry):
    """Models self-register on class creation; do not hand-fill _MODEL_REGISTRY_PY."""
    from ferro import BackRef
    from ferro.relations import resolve_relationships

    class Child(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        parent: Annotated["Parent", ForeignKey(related_name="children")]

    class Parent(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        children: BackRef[list[Child]] = None

    assert is_fallback_shadow_annotation(Child.__annotations__["parent_id"])

    resolve_relationships()

    assert Child.__annotations__["parent_id"] == (int | None)
```

- **Step 2: Run tests (expect failures until Task 3–4 wire imports)**

```bash
cd /Users/taylorroberts/GitHub/syn54x/ferro && uv run pytest tests/test_shadow_fk_types.py -v --tb=short
```

Expected: failures until `reconcile` is invoked from `resolve_relationships` and `UUID` model registers `ferro_fields` correctly — you may need to fix `test_pk_python_type_for_model_uuid` if `Field(primary_key=True)` does not populate `ferro_fields` the same as `FerroField`; align with how issue #16 declares models (`Field` vs `FerroField`). Inspect `Child.ferro_fields` in a REPL if needed.

- **Step 3: Commit after all tests green**

```bash
git add tests/test_shadow_fk_types.py
git commit -m "test: cover shadow FK type helpers and forward-ref reconciliation"
```

---

### Task 3: Metaclass — unwrap optional FK/M2M target + inject shadow from helper

**Files:**

- Modify: `src/ferro/metaclass.py` (imports top; `_scan_relationship_annotations` ~165–205; `_inject_shadow_fields` ~207–222)
- **Step 1: Imports**

Add:

```python
from ._shadow_fk_types import shadow_annotation_for_foreign_key
```

- **Step 2: ForeignKey branch in `_scan_relationship_annotations`**

Replace only the assignment `metadata.to = args[0]` with optional stripping; **keep** `_PENDING_RELATIONS.append`, `local_relations[field_name] = metadata`, `fields_to_remove.append`, and `break`:

```python
                for metadata in args:
                    if isinstance(metadata, ForeignKey):
                        inner = ModelMetaclass._strip_optional_union(args[0])
                        metadata.to = inner
                        local_relations[field_name] = metadata
                        _PENDING_RELATIONS.append((model_name, field_name, metadata))
                        fields_to_remove.append(field_name)
                        break
```

- **Step 3: ManyToManyField inner target**

After resolving `metadata.to` from `list[...]` or plain `args[0]`, if the result is not a list origin, still apply `_strip_optional_union` to the inner model type:

```python
                    if isinstance(metadata, ManyToManyField):
                        origin_inner = get_origin(args[0])
                        if origin_inner is list:
                            inner_args = get_args(args[0])
                            if inner_args:
                                metadata.to = ModelMetaclass._strip_optional_union(
                                    inner_args[0]
                                )
                        else:
                            metadata.to = ModelMetaclass._strip_optional_union(args[0])
                        local_relations[field_name] = metadata
                        _PENDING_RELATIONS.append((model_name, field_name, metadata))
                        fields_to_remove.append(field_name)
                        break
```

- **Step 4: `_inject_shadow_fields`**

Replace `annotations[id_field] = Union[int, str, None]` with:

```python
                annotations[id_field] = shadow_annotation_for_foreign_key(metadata)
```

Keep `namespace[id_field] = PydanticField(default=None)` unchanged.

- **Step 5: Run targeted tests**

```bash
uv run pytest tests/test_metaclass_internals.py::TestInjectShadowFields -v --tb=short
uv run pytest tests/test_relationship_engine.py -v --tb=short
```

- **Step 6: Commit**

```bash
git add src/ferro/metaclass.py
git commit -m "feat(metaclass): derive shadow FK types and unwrap optional relation targets"
```

---

### Task 4: `resolve_relationships` — reconcile + schema pass

**Files:**

- Modify: `src/ferro/relations/__init__.py`
- **Step 1: Import**

```python
from .._shadow_fk_types import reconcile_shadow_fk_types
```

- **Step 2: After the `for model_name, field_name, rel in to_process:` loop ends, before the “Second pass: Re-register schemas” comment**

Insert:

```python
    reconcile_shadow_fk_types(_MODEL_REGISTRY_PY)
```

- **Step 3: Run full relationship + shadow tests**

```bash
uv run pytest tests/test_relationship_engine.py tests/test_shadow_fk_types.py tests/test_metaclass_internals.py -v --tb=short
```

Expected: green. If `__ferro_schema__` stale after rebuild, the existing second pass already calls `model_json_schema()` again — verify `tests/test_alembic_bridge.py` if present after full suite.

- **Step 4: Commit**

```bash
git add src/ferro/relations/__init__.py
git commit -m "feat(relations): reconcile shadow FK types after forward refs resolve"
```

---

### Task 5: Update metaclass internal tests for concrete `int | None` PK

**Files:**

- Modify: `tests/test_metaclass_internals.py`
- **Step 1: Add minimal concrete Parent model in test module** (or reuse pattern from `test_relationship_engine`) so `ForeignKey.to` is a real class with `int | None` PK.
- **Step 2: Change `test_foreign_key_injects_id_field` assertion**

From:

```python
assert annotations["owner_id"] == Union[int, str, None]
```

To:

```python
assert annotations["owner_id"] == (int | None)
```

only if the fake `Owner` model in that test exposes `ferro_fields` with `primary_key` on `id`. If the test uses `fk.to = "User"` string only, **keep** fallback expectation `Union[int, str, None]` (unresolved `to`).

- **Step 3: Run**

```bash
uv run pytest tests/test_metaclass_internals.py::TestInjectShadowFields -v
```

- **Step 4: Commit**

```bash
git add tests/test_metaclass_internals.py
git commit -m "test: align shadow FK inject tests with PK-derived annotations"
```

---

### Task 6: Async integration — issue #16 reproducer (UUID)

**Files:**

- Modify: `tests/test_shadow_fk_types.py` (or new `tests/test_shadow_fk_uuid_integration.py`)
- **Step 1: Add async test** — follow `tests/test_connection.py` / `tests/test_auto_migrate.py`: call `await connect("sqlite::memory:", auto_migrate=True)` directly (no `db_engine` fixture is required; `conftest.py` only defines `db_engine` and it is unused elsewhere). At the **start** of the test call `ferro.reset_engine()` and `ferro.clear_registry()` so dynamically defined models do not collide with other tests. Use **unique model class names** (e.g. `UuidIssueParent`, `UuidIssueChild`) to avoid registry clashes.

```python
import warnings
from typing import Annotated
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import BackRef, Field, ForeignKey, Model, connect


@pytest.mark.asyncio
async def test_uuid_fk_create_get_dump():
    """Regression for GitHub #16: UUID PK through shadow FK without validation/serialization issues."""
    ferro.reset_engine()
    ferro.clear_registry()

    class UuidIssueParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        children: BackRef[list["UuidIssueChild"]] = None

    class UuidIssueChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated[UuidIssueParent, ForeignKey(related_name="children")]

    await connect("sqlite::memory:", auto_migrate=True)

    parent = await UuidIssueParent.create(name="p")
    child = await UuidIssueChild.create(parent=parent)

    fetched = await UuidIssueChild.get(child.id)
    assert fetched.parent_id == parent.id

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        fetched.model_dump_json()
    unexpected = [
        x
        for x in w
        if x.category.__name__ == "PydanticSerializationUnexpectedValue"
    ]
    assert not unexpected
```

- **Step 2: Run**

```bash
uv run maturin develop && uv run pytest tests/test_shadow_fk_types.py::test_uuid_fk_create_get_dump -v --tb=short
```

- **Step 3: Commit**

```bash
git add tests/test_shadow_fk_types.py
git commit -m "test: UUID foreign key create/get/serialize integration"
```

---

### Task 7: Nullable `Annotated[Parent | None, ForeignKey]` regression

**Files:**

- Modify: `tests/test_shadow_fk_types.py`
- **Step 1: Test model** (in-memory registry; call `resolve_relationships`)

```python
def test_nullable_fk_annotation_does_not_crash():
    import ferro
    from ferro import BackRef, Field, ForeignKey, Model
    from ferro.relations import resolve_relationships

    ferro.clear_registry()

    class Parent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        children: BackRef[list["Child"]] = None

    class Child(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated[Parent | None, ForeignKey(related_name="children")] = None

    resolve_relationships()
    assert Child.ferro_relations["parent"].to is Parent
```

- **Step 2: Run + commit**

```bash
uv run pytest tests/test_shadow_fk_types.py::test_nullable_fk_annotation_does_not_crash -v
git add tests/test_shadow_fk_types.py && git commit -m "test: nullable FK annotated target unwraps cleanly"
```

---

### Task 8: Full verification

- **Step 1: Lint + full test suite**

```bash
cd /Users/taylorroberts/GitHub/syn54x/ferro
uv run ruff check src tests
uv run maturin develop
uv run pytest -q
```

Expected: all pass; `cargo test` optional if Rust untouched.

- **Step 2: Commit** (only if fixes needed)

```bash
git add -A && git commit -m "fix: ruff/pytest cleanup for shadow FK types"
```

---

## Spec coverage (self-review)


| Spec section                                     | Task(s)                                       |
| ------------------------------------------------ | --------------------------------------------- |
| PK type helper                                   | Task 1                                        |
| `_inject_shadow_fields` concrete vs fallback     | Tasks 1, 3                                    |
| `Target | None` unwrap for FK (and M2M inner)    | Task 3                                        |
| Post-`resolve_relationships` reconcile + rebuild | Tasks 1 (reconcile), 4                        |
| Rust / `register_model_schema` refresh           | Task 4 (existing second pass after reconcile) |
| UUID repro + forward ref + nullable FK tests     | Tasks 2, 5, 6, 7                              |
| Metaclass / relationship regressions             | Tasks 3–5, 8                                  |


**Placeholder scan:** None intentional; replace `...` and duplicate-class mistakes in Task 6 before implementation.

**Consistency:** Single helper module name `_shadow_fk_types.py`; public functions as listed; `ForeignKey` import inside `shadow_annotation_for_foreign_key` / `reconcile_shadow_fk_types` only to avoid import cycles. PK annotations strip `Annotated[..., FerroField]` to the inner scalar before building shadow `| None`.

---

## Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-22-shadow-fk-types.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration. **REQUIRED SUB-SKILL:** superpowers:subagent-driven-development.
2. **Inline execution** — Run tasks in this session with checkpoints. **REQUIRED SUB-SKILL:** superpowers:executing-plans.

**Which approach do you want?**
