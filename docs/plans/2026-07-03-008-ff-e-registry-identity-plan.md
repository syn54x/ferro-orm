# FF-E Registry & Model Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Qualified model identity + table-name collision detection at class definition, configurable `__ferro_table__`, O(N) import cost, and loud relationship re-registration failures (findings F9/F11; Epic FF-E).

**Architecture:** Per `docs/plans/2026-07-03-007-ff-e-registry-identity-design.md`. The metaclass stamps `cls.__ferro_identity__` (`module.qualname`, the registry key on both sides of the FFI) and `cls.__ferro_table__` (resolved physical table). The Rust `RegisteredModel` carries `table_name`; every `name.to_lowercase()` table derivation becomes a registry read (RouteHandle-style: resolve once, read everywhere). Collision detection keys on the resolved table name; same-identity redefinition stays idempotent.

**Tech Stack:** Python 3.12+ (Pydantic v2 metaclass), Rust (PyO3, sea-query), maturin, pytest, cargo test.

## Global Constraints

- In-development version is **0.13.0** (FF-D shipped as 0.13.0); FF-E's breaking commits (`feat(ff-e)!`) cut 0.14.0 at release. Do not edit version files.
- Conventional commits, standard types only (commitizen/CI reject invented types), scope `ff-e`, `!` on user-observable breaking commits. **No AI attribution** in commits or PR (AGENTS.md I-6). Local commit hook may be missing — validate format yourself.
- After every Rust change: `uv run maturin develop`. No panics across the FFI boundary — `PyResult` everywhere, no `unwrap()` on Python-facing data (AGENTS.md I-3).
- **Never bulk-reformat** — `cargo fmt` / `ruff format` flag `main` itself locally; hand-format only your own hunks (compare against `main`).
- Tests declaring models inside functions rely on registry isolation; after Task 5 a global autouse fixture in `tests/conftest.py` provides it.
- Env: `FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres` (docker `local-pg`). Full matrix: `FERRO_POSTGRES_URL=… just test` — green on `main`; any failure is a real regression.
- Docs: field-declaring examples show **both** Assignment + Annotated tabs (I-8); query examples use lambda predicates (I-9); plain-language + example-first (I-11).
- Keep registry key and table name distinct concepts everywhere. The key is `__ferro_identity__`; the table is `__ferro_table__`. Never lowercase a key to get a table.

---

### Task 1: E4 — `resolve_relationships` second pass fails loudly

**Files:**
- Modify: `src/ferro/relations/__init__.py:144-150`
- Test: `tests/test_relationship_resolution_errors.py` (create)

**Interfaces:**
- Produces: the second-pass loop raises `RuntimeError` naming the failing model; no `except Exception: pass` remains in `resolve_relationships`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_relationship_resolution_errors.py`:

```python
"""FF-E E4: resolve_relationships' schema re-registration pass fails loudly.

The second pass used to wrap every re-registration in `except Exception:
pass`, leaving a model that failed to rebuild silently on its
pre-relationship schema.
"""

import pytest

from ferro import Model
from ferro.relations import resolve_relationships


@pytest.fixture(autouse=True)
def _isolate_relation_state():
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    models_snapshot = dict(_MODEL_REGISTRY_PY)
    relations_snapshot = list(_PENDING_RELATIONS)
    yield
    _MODEL_REGISTRY_PY.clear()
    _MODEL_REGISTRY_PY.update(models_snapshot)
    _PENDING_RELATIONS.clear()
    _PENDING_RELATIONS.extend(relations_snapshot)


def test_second_pass_rebuild_failure_aborts_with_model_named(monkeypatch):
    class E4Broken(Model):
        id: int | None = None
        name: str

    import ferro.relations as relations_mod

    real_build = relations_mod.build_model_schema

    def failing_build(model_cls, schema=None):
        if model_cls.__name__ == "E4Broken":
            raise ValueError("boom: schema rebuild failed")
        return real_build(model_cls)

    monkeypatch.setattr(relations_mod, "build_model_schema", failing_build)

    with pytest.raises(RuntimeError) as excinfo:
        resolve_relationships()

    assert "E4Broken" in str(excinfo.value)
    assert "boom: schema rebuild failed" in str(excinfo.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_relationship_resolution_errors.py -v`
Expected: FAIL — `resolve_relationships()` swallows the error, no exception raised.

- [ ] **Step 3: Make the second pass raise**

In `src/ferro/relations/__init__.py`, replace the second-pass loop:

```python
    # Second pass: Re-register schemas — loudly (FF-E E4). A model whose
    # schema fails to rebuild here would otherwise be silently left on its
    # pre-relationship schema.
    for model_name, model_cls in _MODEL_REGISTRY_PY.items():
        try:
            schema = build_model_schema(model_cls)
        except Exception as exc:
            raise RuntimeError(
                f"Ferro failed to rebuild the schema for model '{model_name}' "
                f"while resolving relationships: {exc}"
            ) from exc
        register_model_schema(model_name, json.dumps(schema))
```

(`register_model_schema` errors propagate unwrapped — they are already actionable `PyErr`s.)

- [ ] **Step 4: Run test to verify it passes, plus the suite's relationship tests**

Run: `uv run pytest tests/test_relationship_resolution_errors.py tests/test_relationship_engine.py tests/test_fk_target_pk.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ferro/relations/__init__.py tests/test_relationship_resolution_errors.py
git commit -m "fix(ff-e): E4 — resolve_relationships re-registration fails loudly

The second pass swallowed every schema-rebuild failure with
'except Exception: pass', leaving the model silently on its
pre-relationship schema. It now raises RuntimeError naming the model,
matching the first-pass loop's behavior."
```

---

### Task 2: Stamp `__ferro_identity__` / `__ferro_table__`; add `resolve_model_reference`

**Files:**
- Modify: `src/ferro/metaclass.py` (`__new__` Phase 3, new `_resolve_table_name` helper)
- Modify: `src/ferro/state.py` (add `resolve_model_reference`)
- Test: `tests/test_model_identity.py` (create)

**Interfaces:**
- Produces: `cls.__ferro_identity__: str` = `f"{cls.__module__}.{cls.__qualname__}"`; `cls.__ferro_table__: str` = validated configured value (own class body only) or `cls.__name__.lower()`.
- Produces: `ferro.state.resolve_model_reference(ref: str, *, default=...) -> type` — exact identity-key hit, else unique bare-class-name match, else ambiguity `RuntimeError` listing qualified candidates, else not-found `RuntimeError` (or `default` when given). Ambiguity raises even when `default` is given.
- Registry keys are NOT changed in this task (still bare class name).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_model_identity.py`:

```python
"""FF-E E1/E2: model identity stamps and reference resolution."""

from typing import ClassVar

import pytest

from ferro import Model
from ferro.state import resolve_model_reference


@pytest.fixture(autouse=True)
def _isolate_relation_state():
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    models_snapshot = dict(_MODEL_REGISTRY_PY)
    relations_snapshot = list(_PENDING_RELATIONS)
    yield
    _MODEL_REGISTRY_PY.clear()
    _MODEL_REGISTRY_PY.update(models_snapshot)
    _PENDING_RELATIONS.clear()
    _PENDING_RELATIONS.extend(relations_snapshot)


def test_models_are_stamped_with_identity_and_table():
    class IdentityUser(Model):
        id: int | None = None

    assert IdentityUser.__ferro_identity__ == (
        f"{IdentityUser.__module__}.{IdentityUser.__qualname__}"
    )
    assert IdentityUser.__ferro_table__ == "identityuser"


def test_ferro_table_overrides_default():
    class TableUser(Model):
        __ferro_table__: ClassVar[str] = "app_users"
        id: int | None = None

    assert TableUser.__ferro_table__ == "app_users"


def test_ferro_table_is_not_inherited():
    class TableParent(Model):
        __ferro_table__: ClassVar[str] = "custom_parent"
        id: int | None = None

    class TableChild(TableParent):
        pass

    assert TableChild.__ferro_table__ == "tablechild"


def test_ferro_table_rejects_non_string():
    with pytest.raises(TypeError, match="__ferro_table__"):

        class BadType(Model):
            __ferro_table__: ClassVar[int] = 7
            id: int | None = None


@pytest.mark.parametrize("bad", ["", "1starts_with_digit", "has space", "x" * 64])
def test_ferro_table_rejects_invalid_names(bad):
    with pytest.raises(ValueError, match="__ferro_table__"):
        type("BadTable", (Model,), {"__ferro_table__": bad, "__annotations__": {"id": int | None}, "id": None})


def test_resolve_model_reference_short_and_qualified():
    class RefTarget(Model):
        id: int | None = None

    assert resolve_model_reference("RefTarget") is RefTarget
    assert resolve_model_reference(RefTarget.__ferro_identity__) is RefTarget


def test_resolve_model_reference_not_found():
    with pytest.raises(RuntimeError, match="NoSuchModelAnywhere"):
        resolve_model_reference("NoSuchModelAnywhere")
    assert resolve_model_reference("NoSuchModelAnywhere", default=None) is None
```

(An ambiguity test is added in Task 6 once qualified keys allow two same-named models to coexist.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model_identity.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_model_reference'` / `AttributeError: __ferro_identity__`.

- [ ] **Step 3: Implement stamping in the metaclass**

In `src/ferro/metaclass.py`, add near the top (after imports):

```python
import re

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
```

Add a static method on `ModelMetaclass`:

```python
    @staticmethod
    def _resolve_table_name(name: str, namespace: dict) -> str:
        """Resolve the physical table name for a model class (FF-E E2).

        ``__ferro_table__`` is honored only when declared in the class's own
        body (the metaclass namespace) — a subclass never silently inherits
        its parent's physical table. Default: ``classname.lower()``.
        """
        configured = namespace.get("__ferro_table__")
        if configured is None:
            return name.lower()
        if not isinstance(configured, str):
            raise TypeError(
                f"__ferro_table__ on model '{name}' must be a str, "
                f"got {type(configured).__name__}"
            )
        if len(configured) > 63 or not _TABLE_NAME_RE.match(configured):
            raise ValueError(
                f"__ferro_table__ {configured!r} on model '{name}' is not a "
                "valid table name: expected a 1-63 character identifier "
                "matching [A-Za-z_][A-Za-z0-9_]*"
            )
        return configured
```

In `ModelMetaclass.__new__`, immediately after the `if name == "Model": return cls` early return, stamp both attributes before `_register_model_and_proxies`:

```python
        cls.__ferro_identity__ = f"{cls.__module__}.{cls.__qualname__}"
        cls.__ferro_table__ = mcs._resolve_table_name(name, namespace)

        mcs._register_model_and_proxies(cls, name, local_relations)
```

Note: stamping must use `cls.__module__`/`cls.__qualname__` (post-creation), not the namespace — `type(...)`-built classes get `__module__` filled in by `type.__new__`.

- [ ] **Step 4: Implement `resolve_model_reference` in `src/ferro/state.py`**

After the `_MODEL_REGISTRY_PY` definition:

```python
_UNSET = object()


def resolve_model_reference(ref: str, *, default: Any = _UNSET) -> Any:
    """Resolve a model reference to a registered model class (FF-E E1).

    Accepts a qualified identity (``module.QualName``) or a bare class name.
    A bare name matching exactly one registered model resolves to it; several
    matches raise with the qualified candidates listed; no match raises
    (or returns ``default`` when given). Ambiguity always raises.
    """
    model = _MODEL_REGISTRY_PY.get(ref)
    if model is not None:
        return model
    candidates = [cls for cls in _MODEL_REGISTRY_PY.values() if cls.__name__ == ref]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        listed = ", ".join(
            sorted(getattr(c, "__ferro_identity__", c.__qualname__) for c in candidates)
        )
        raise RuntimeError(
            f"Model reference '{ref}' is ambiguous: {listed}. "
            "Use the qualified 'module.QualName' form to disambiguate."
        )
    if default is not _UNSET:
        return default
    raise RuntimeError(f"Model '{ref}' not found in registry")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_model_identity.py tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ferro/metaclass.py src/ferro/state.py tests/test_model_identity.py
git commit -m "feat(ff-e): stamp __ferro_identity__/__ferro_table__; add resolve_model_reference

Every model class now carries its qualified identity (module.qualname)
and its resolved physical table name (validated __ferro_table__ from the
class's own body, or classname.lower()). resolve_model_reference resolves
qualified or unambiguous bare names and errors listing candidates on
ambiguity. Registry keys are unchanged in this commit."
```

---

### Task 3: E2 — table name through the FFI; kill every Rust `to_lowercase()` table derivation

**Files:**
- Modify: `src/state.rs` (`RegisteredModel` + `registered_model` helper)
- Modify: `src/schema.rs` (`register_model_schema` 3-arg; `order_schemas_for_creation`)
- Modify: `src/operations.rs` (11 CRUD sites + legacy query path ~:2947)
- Modify: `src/migrate.rs` (~:822, :959, :1026)
- Modify: `src/ferro/ir/compiler.py` (`compile_model_schema_ir` passes `table_name`)
- Modify: `src/ferro/metaclass.py:573`, `src/ferro/relations/__init__.py:139,148`, `src/ferro/models.py:211` (3-arg `register_model_schema` callers)
- Modify: `src/ferro/_core.pyi:25`
- Test: `tests/test_naming_single_source.py` (add custom-table round-trip test)

**Interfaces:**
- Consumes: `cls.__ferro_table__` from Task 2.
- Produces: `register_model_schema(name: str, schema: str, table_name: str)` (FFI, three required args); `RegisteredModel { schema, table_name, codec_plan }`; `crate::state::registered_model(name: &str) -> PyResult<Arc<RegisteredModel>>`. Rust never derives a table name from a model name again.

- [ ] **Step 1: Write the failing exit-gate test**

Append to `tests/test_naming_single_source.py`:

```python
def test_custom_table_name_round_trips_both_emitters():
    """FF-E E2 exit gate: __ferro_table__ flows into the IR and both emitters."""
    import json

    from ferro._core import _render_create_table_sql_for_test

    class BillingAccount(Model):
        __ferro_table__: ClassVar[str] = "billing_accounts"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]

    envelope = compile_registry_schema_ir()
    model = next(
        m for m in envelope["payload"]["models"] if m["table_name"] == "billing_accounts"
    )
    assert _ddl_single_unique_name("billing_accounts", "email") in {
        u["name"] for u in model["uniques"]
    }

    payload_json = json.dumps(envelope["payload"])
    for dialect in ("sqlite", "postgres"):
        create_sql, _post, _pre = _render_create_table_sql_for_test(
            "billing_accounts", payload_json, dialect
        )
        assert '"billing_accounts"' in create_sql, (dialect, create_sql)
        assert '"billingaccount"' not in create_sql

    metadata = get_metadata()
    assert "billing_accounts" in metadata.tables
    assert "billingaccount" not in metadata.tables
```

Run: `uv run pytest tests/test_naming_single_source.py::test_custom_table_name_round_trips_both_emitters -v`
Expected: FAIL — IR `table_name` is `billingaccount` (the lowercased class name).

- [ ] **Step 2: Rust — `RegisteredModel` carries `table_name`; add `registered_model` helper**

In `src/state.rs`:

```rust
#[derive(Debug)]
pub struct RegisteredModel {
    /// The enriched Pydantic JSON schema as pushed from Python.
    pub schema: serde_json::Value,
    /// Resolved physical table name (configured `__ferro_table__` or the
    /// lowercased class name), pushed from Python at registration (FF-E E2).
    /// Rust never derives a table name from a model name.
    pub table_name: String,
    /// Per-column codec decisions, compiled once at registration.
    pub codec_plan: crate::codec_plan::ModelCodecPlan,
}

impl RegisteredModel {
    /// Compile the codec plan and wrap the registration for the registry.
    pub fn new(schema: serde_json::Value, table_name: String) -> Arc<Self> {
        let codec_plan = crate::codec_plan::ModelCodecPlan::compile(&schema);
        Arc::new(RegisteredModel { schema, table_name, codec_plan })
    }
}

/// Look up one registration by registry key.
///
/// # Errors
/// `PyRuntimeError` when the registry lock fails or the model is unknown.
pub fn registered_model(name: &str) -> PyResult<Arc<RegisteredModel>> {
    MODEL_REGISTRY
        .read()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?
        .get(name)
        .cloned()
        .ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
        })
}
```

- [ ] **Step 3: Rust — `register_model_schema` takes `table_name`; ordering compares table names**

In `src/schema.rs`:

```rust
#[pyfunction]
#[pyo3(signature = (name, schema, table_name))]
pub fn register_model_schema(name: String, schema: String, table_name: String) -> PyResult<()> {
    let parsed_schema: serde_json::Value = serde_json::from_str(&schema).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON schema: {}", e))
    })?;
    if table_name.is_empty() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "table_name must be a non-empty string",
        ));
    }

    let mut registry = MODEL_REGISTRY
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?;

    registry.insert(
        name.clone(),
        crate::state::RegisteredModel::new(parsed_schema, table_name),
    );
    crate::log_debug(format!("⚙️  Ferro Engine: Map generated for '{}'", name));
    Ok(())
}
```

In `order_schemas_for_creation`, replace the two lowercase sites:

```rust
        let available_names: HashSet<String> = remaining
            .iter()
            .map(|(_, model)| model.table_name.clone())
            .collect();
```

and

```rust
                let table = item.1.table_name.clone();
                created.insert(table);
```

- [ ] **Step 4: Rust — replace every table derivation in `operations.rs` and `migrate.rs`**

Pattern (identical at all 11 CRUD sites — `fetch_all` :930, `fetch_one` :1072, `save_record` :1413, `update_record` :1530, `save_bulk_records` :1645, `fetch_filtered` :1726, `count_filtered` :1916, `delete_record` :2083, `delete_filtered` :2167, `update_filtered` :2236; line numbers drift — let `cargo build` and `grep -n 'name.to_lowercase()' src/operations.rs` enumerate):

```rust
// BEFORE
let table_name = name.to_lowercase();
// ...
let registry = MODEL_REGISTRY.read().map_err(|_| { ... })?;
let schema = registry.get(&name).ok_or_else(|| { ... })?;

// AFTER — one registry read supplies both
let schema = crate::state::registered_model(&name)?;
let table_name = schema.table_name.clone();
```

Where the site currently reads the registry *after* deriving the table (e.g. `fetch_all`), hoist the `registered_model` call above the first `table_name` use. Where a site holds the registry lock in a block that returns `(sql, ...)`, replace the whole lock+get with the helper.

Legacy query path (~:2947): `QueryDef.registration` is already resolved; error loudly when absent:

```rust
    let legacy_table = query_def
        .registration
        .as_ref()
        .map(|r| r.table_name.clone())
        .ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Model '{}' not found",
                query_def.model_name
            ))
        })?;
    let mut select_legacy = Query::select();
    select_legacy.from(Alias::new(&legacy_table));
    select_legacy.column((Alias::new(&legacy_table), sea_query::Asterisk));
```

`src/migrate.rs` ~:822 (runtime migrate loop over `order_schemas_for_creation`):

```rust
    for (name, model) in order_schemas_for_creation(schemas) {
        let schema = &model.schema;
        let table_lower = model.table_name.clone();
```

`src/migrate.rs` ~:959 (`_render_migration_sql_for_test`) and ~:1026 (`_shadow_compare_migration_plan_for_test`): these are test-support FFI surfaces whose `name` parameter now means the **table name** directly — delete the `.to_lowercase()`:

```rust
    let table_lower = name;
```

(Existing Python callers in `tests/test_migrate_plan.py` / `tests/test_shadow_reports.py` already pass lowercase table names, so behavior is unchanged; verify with the suite.)

Do NOT touch: `order.direction.to_lowercase()` (:1782, :2955 — SQL direction parsing), `src/introspect.rs` / `src/query.rs` test asserts, `src/schema.rs:509` (`_render_create_table_sql_for_test`'s match-helper fallback stays tolerant).

- [ ] **Step 5: Python — pass the table name at every `register_model_schema` call; IR compiler uses `__ferro_table__`**

`src/ferro/metaclass.py` `_generate_and_register_schema`:

```python
                register_model_schema(name, json.dumps(schema), cls.__ferro_table__)
```

`src/ferro/relations/__init__.py` — join table (:139):

```python
            register_model_schema(join_table, json.dumps(join_schema), join_table)
```

and the second pass (from Task 1):

```python
        register_model_schema(
            model_name, json.dumps(schema), model_cls.__ferro_table__
        )
```

`src/ferro/models.py` `_reregister_ferro`:

```python
            register_model_schema(
                cls.__name__, json.dumps(schema), cls.__ferro_table__
            )
```

`src/ferro/ir/compiler.py` `compile_model_schema_ir`:

```python
def compile_model_schema_ir(model_name: str, model_cls: type[Any]) -> dict[str, Any]:
    schema = build_model_schema(model_cls)
    payload = compile_schema_ir_payload(
        model_name,
        schema,
        table_name=getattr(model_cls, "__ferro_table__", None),
    )
```

`src/ferro/_core.pyi:25`:

```python
def register_model_schema(name: str, schema: str, table_name: str) -> None: ...
```

- [ ] **Step 6: Rebuild, run the exit-gate test and the suite**

Run:
```bash
uv run maturin develop
uv run pytest tests/test_naming_single_source.py tests/test_models.py tests/test_crud.py tests/test_migrate_plan.py -x -q
grep -n 'to_lowercase()' src/operations.rs src/migrate.rs src/schema.rs
```
Expected: tests PASS; grep shows only `order.direction.to_lowercase()` sites and `schema.rs:509`'s match-helper.

- [ ] **Step 7: Run the full local suite**

Run: `uv run pytest tests/ -x -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/state.rs src/schema.rs src/operations.rs src/migrate.rs \
    src/ferro/ir/compiler.py src/ferro/metaclass.py src/ferro/relations/__init__.py \
    src/ferro/models.py src/ferro/_core.pyi tests/test_naming_single_source.py
git commit -m "feat(ff-e)!: E2 — resolved table name lives on the registration, not derived

register_model_schema now takes the resolved table name (from
__ferro_table__ or the classname.lower() default) and stores it on
RegisteredModel. Every Rust name.to_lowercase() table derivation — 11
CRUD sites, the legacy query path, the migrate loop, and creation
ordering — becomes a registry read. The IR compiler passes the
configured table into compile_schema_ir_payload.

BREAKING CHANGE: the register_model_schema FFI takes a third required
table_name argument."
```

---

### Task 4: E2 — FK `to_table` and M2M join artifacts follow configured table names

**Files:**
- Modify: `src/ferro/relations/__init__.py:67-140` (join table, source/target cols, `to_table` strings)
- Modify: `src/ferro/schema_metadata.py:68-75` (`_target_table_name`)
- Test: `tests/test_naming_single_source.py` (relation + M2M to custom-table models)

**Interfaces:**
- Consumes: `__ferro_table__` (Task 2), `resolve_model_reference` (Task 2).
- Produces: every FK/M2M `to_table`, join-table name, and join column derives from the participants' `__ferro_table__`; byte-identical strings for default-named models.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_naming_single_source.py`:

```python
def test_relation_to_custom_table_model_points_at_configured_table():
    """FF-E E2 exit gate: FK to_table follows the target's __ferro_table__."""

    class OrgTeam(Model):
        __ferro_table__: ClassVar[str] = "org_teams"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        players: Relation[list["TeamPlayer"]] = BackRef()

    class TeamPlayer(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        team: Annotated[OrgTeam, ForeignKey(related_name="players")]

    envelope = compile_registry_schema_ir()
    player = next(
        m for m in envelope["payload"]["models"] if m["table_name"] == "teamplayer"
    )
    (fk,) = player["foreign_keys"]
    assert fk["to_table"] == "org_teams"
    assert fk["name"] == _ddl_fk_name("teamplayer", "team_id", "org_teams")

    metadata = get_metadata()
    fk_constraints = [
        c
        for c in metadata.tables["teamplayer"].constraints
        if isinstance(c, sa.ForeignKeyConstraint)
    ]
    (fk_element,) = fk_constraints[0].elements
    assert fk_element.target_fullname == "org_teams.id"


def test_m2m_join_artifacts_follow_custom_table_names():
    """FF-E E2 exit gate: default join table/columns derive from table names."""
    from ferro import ManyToMany
    from ferro.relations import resolve_relationships
    from ferro.state import _JOIN_TABLE_REGISTRY

    class WikiPage(Model):
        __ferro_table__: ClassVar[str] = "wiki_pages"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        tags: Relation[list["WikiTag"]] = ManyToMany(related_name="pages")

    class WikiTag(Model):
        __ferro_table__: ClassVar[str] = "wiki_tags"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        pages: Relation[list[WikiPage]] = BackRef()

    resolve_relationships()

    assert "wiki_pages_tags" in _JOIN_TABLE_REGISTRY
    join_schema = _JOIN_TABLE_REGISTRY["wiki_pages_tags"]
    props = join_schema["properties"]
    assert set(props) == {"wiki_pages_id", "wiki_tags_id"}
    assert props["wiki_pages_id"]["foreign_key"]["to_table"] == "wiki_pages"
    assert props["wiki_tags_id"]["foreign_key"]["to_table"] == "wiki_tags"
```

Run: `uv run pytest tests/test_naming_single_source.py -k 'custom_table or m2m_join' -v`
Expected: FAIL — `to_table` is the lowercased class name (`orgteam`, `wikipage`...), join table is `wikipage_tags`.

- [ ] **Step 2: Fix `resolve_relationships` M2M derivation**

In `src/ferro/relations/__init__.py`, inside the `ManyToManyRelation` branch, replace the class-name derivations:

```python
        elif isinstance(rel, ManyToManyRelation):
            source_model = _MODEL_REGISTRY_PY[model_name]
            source_table = source_model.__ferro_table__
            target_table = target_model.__ferro_table__

            # Resolve join table
            if not rel.through:
                # Default join table name: source table + field name.
                join_table = f"{source_table}_{field_name}"
            else:
                join_table = rel.through

            source_col = f"{source_table}_id"
            target_col = f"{target_table}_id"
```

and in the join schema `foreign_key` blocks:

```python
                        "foreign_key": {
                            "to_table": source_table,
                            "on_delete": "CASCADE",
                        },
```

```python
                        "foreign_key": {
                            "to_table": target_table,
                            "on_delete": "CASCADE",
                        },
```

(The `_MODEL_REGISTRY_PY[model_name]` source lookup replaces the two existing inline `_MODEL_REGISTRY_PY[model_name]` reads in the descriptor-injection and PK-schema lines — use `source_model` there.)

- [ ] **Step 3: Fix `_target_table_name` in `src/ferro/schema_metadata.py`**

```python
def _target_table_name(target: Any) -> str:
    from .state import resolve_model_reference

    if isinstance(target, ForwardRef):
        target = target.__forward_arg__
    if isinstance(target, str):
        model = resolve_model_reference(target, default=None)
        if model is not None:
            return model.__ferro_table__
        # Provisional first-pass fallback for a not-yet-defined forward ref:
        # resolve_relationships' second pass (loud since FF-E E4) re-registers
        # with the target's real table before any DDL consumer runs.
        return target.lower()
    table = getattr(target, "__ferro_table__", None)
    if isinstance(table, str):
        return table
    if hasattr(target, "__name__"):
        return target.__name__.lower()
    return str(target).lower()
```

(Function-local import avoids a module cycle: `state` must stay import-light.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_naming_single_source.py tests/test_relationship_engine.py tests/test_m2m*.py -q`
(then `uv run pytest tests/ -x -q`)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ferro/relations/__init__.py src/ferro/schema_metadata.py tests/test_naming_single_source.py
git commit -m "feat(ff-e): E2 — FK to_table and M2M join artifacts follow configured tables

The hand-built M2M join-table name, join columns, and both to_table
strings now derive from each participant's __ferro_table__, and
_target_table_name resolves class/string targets to the configured
table. Byte-identical output for default-named models."
```

---

### Task 5: Test-suite registry isolation + module-scope duplicate renames

**Files:**
- Modify: `tests/conftest.py` (global autouse snapshot/restore fixture)
- Modify: `tests/test_schema.py`, `tests/test_documentation_features.py` (rename module-scope `User`/`Post`/`Product` duplicates; `tests/test_relationship_engine.py` keeps the canonical names)

**Interfaces:**
- Produces: every test runs inside a `_MODEL_REGISTRY_PY`/`_PENDING_RELATIONS`/`_JOIN_TABLE_REGISTRY` snapshot/restore, so function-local models never leak across tests. Required before Tasks 6–7: qualified keys make same-named models *coexist* (they no longer clobber), and collision detection makes leaks *hard errors*.

- [ ] **Step 1: Add the global autouse fixture to `tests/conftest.py`**

After the existing `cleanup_models` fixture:

```python
@pytest.fixture(autouse=True)
def _ferro_registry_isolation():
    """Snapshot/restore the global model registries around every test (FF-E).

    Function-local test models used to accumulate in the global registries
    for the whole session — harmless under bare-class-name keys (later
    same-named models clobbered earlier ones) but fatal under FF-E's
    qualified keys + table-name collision detection. Module-scope models are
    captured in the baseline snapshot and survive; function-local models are
    dropped when the test ends.
    """
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    models_snapshot = dict(_MODEL_REGISTRY_PY)
    pending_snapshot = list(_PENDING_RELATIONS)
    joins_snapshot = dict(_JOIN_TABLE_REGISTRY)
    yield
    _MODEL_REGISTRY_PY.clear()
    _MODEL_REGISTRY_PY.update(models_snapshot)
    _PENDING_RELATIONS.clear()
    _PENDING_RELATIONS.extend(pending_snapshot)
    _JOIN_TABLE_REGISTRY.clear()
    _JOIN_TABLE_REGISTRY.update(joins_snapshot)
```

- [ ] **Step 2: Rename module-scope duplicates**

Find them:

```bash
grep -rn '^class \(User\|Post\|Product\)(Model' tests/
```

Keep `tests/test_relationship_engine.py`'s names canonical. In `tests/test_schema.py` rename module-scope duplicates with a `Schema` prefix (`User` → `SchemaUser`, etc.); in `tests/test_documentation_features.py` with a `Doc` prefix (`Post` → `DocPost`, etc.). Update every in-file reference (definitions, queries, asserts on table names — the table for `SchemaUser` becomes `schemauser`; adjust any SQL/table-name assertions accordingly).

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: PASS — same count as before this task (no skips introduced).

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py tests/test_schema.py tests/test_documentation_features.py
git commit -m "test(ff-e): isolate the global model registries per test; rename module-scope duplicates

Function-local test models accumulated in the global registries for the
whole session — invisible under bare-name keys because later definitions
clobbered earlier ones (exactly finding F9). Qualified keys + table
collision detection make that leak loud, so a global autouse fixture now
snapshots/restores the registries around every test, and the module-scope
User/Post/Product duplicates across test files get distinct names."
```

---

### Task 6: E1 — qualified registry keys on both sides of the FFI

**Files:**
- Modify: `src/ferro/metaclass.py` (register under identity; pending relations carry identity; `ForwardDescriptor` targets)
- Modify: `src/ferro/relations/__init__.py` (string resolution via `resolve_model_reference`; descriptor `target_model_name` = identity)
- Modify: `src/ferro/relations/descriptors.py` (lazy lookups via `resolve_model_reference`)
- Modify: `src/ferro/models.py` (all FFI name args → `__ferro_identity__`; `evict_instance` accepts class or string; `_reregister_ferro`)
- Modify: `src/ferro/query/builder.py` (QueryIR `model_name` + name-string args → identity)
- Modify: `src/operations.rs` (`fetch_all`/`fetch_one`/`fetch_filtered` extract `__ferro_identity__`; add `model_identity` helper)
- Modify: `src/ferro/ir/compiler.py` (drop the dead `model_name == "Model"` skip)
- Test: `tests/test_model_identity.py` (qualified-key + FK ambiguity tests)

**Interfaces:**
- Consumes: `__ferro_identity__` stamps, `resolve_model_reference`, registry isolation (Task 5).
- Produces: `_MODEL_REGISTRY_PY` and Rust `MODEL_REGISTRY` key by `__ferro_identity__`; QueryIR/SchemaIR `model_name` carries the identity; identity-map keys use the identity; `ferro.evict_instance(model: type[Model] | str, pk, ...)`. Rust helper `crate::state::model_identity(cls: &Bound<PyAny>) -> PyResult<String>`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_model_identity.py`:

```python
def test_registry_keys_by_qualified_identity():
    class QualifiedKeyModel(Model):
        __ferro_table__: ClassVar[str] = "qualified_key_model_a"
        id: int | None = None

    from ferro.state import _MODEL_REGISTRY_PY

    assert _MODEL_REGISTRY_PY[QualifiedKeyModel.__ferro_identity__] is QualifiedKeyModel
    assert "QualifiedKeyModel" not in _MODEL_REGISTRY_PY


def test_same_named_models_in_distinct_scopes_coexist():
    def make_a():
        class ScopedModel(Model):
            __ferro_table__: ClassVar[str] = "scoped_model_a"
            id: int | None = None

        return ScopedModel

    def make_b():
        class ScopedModel(Model):
            __ferro_table__: ClassVar[str] = "scoped_model_b"
            id: int | None = None

        return ScopedModel

    a, b = make_a(), make_b()
    from ferro.state import _MODEL_REGISTRY_PY

    assert _MODEL_REGISTRY_PY[a.__ferro_identity__] is a
    assert _MODEL_REGISTRY_PY[b.__ferro_identity__] is b


def test_resolve_model_reference_ambiguous_lists_candidates():
    def make_a():
        class AmbiguousRef(Model):
            __ferro_table__: ClassVar[str] = "ambiguous_ref_a"
            id: int | None = None

        return AmbiguousRef

    def make_b():
        class AmbiguousRef(Model):
            __ferro_table__: ClassVar[str] = "ambiguous_ref_b"
            id: int | None = None

        return AmbiguousRef

    a, b = make_a(), make_b()
    with pytest.raises(RuntimeError) as excinfo:
        resolve_model_reference("AmbiguousRef")
    message = str(excinfo.value)
    assert a.__ferro_identity__ in message
    assert b.__ferro_identity__ in message


def test_fk_string_ref_to_ambiguous_short_name_errors_with_candidates():
    from typing import Annotated

    from ferro import BackRef, Relation
    from ferro.base import ForeignKey
    from ferro.relations import resolve_relationships

    def make_target(table):
        class FkTarget(Model):
            __ferro_table__: ClassVar[str] = table
            id: int | None = None
            refs: Relation[list["FkSource"]] = BackRef()

        return FkTarget

    t1 = make_target("fk_target_one")
    t2 = make_target("fk_target_two")

    class FkSource(Model):
        id: int | None = None
        target: Annotated["FkTarget", ForeignKey(related_name="refs")]

    with pytest.raises(RuntimeError) as excinfo:
        resolve_relationships()
    message = str(excinfo.value)
    assert t1.__ferro_identity__ in message
    assert t2.__ferro_identity__ in message
```

Run: `uv run pytest tests/test_model_identity.py -v`
Expected: new tests FAIL (registry still keys by bare name; second `ScopedModel` clobbers the first).

- [ ] **Step 2: Python — register under identity; pending relations carry identity**

In `src/ferro/metaclass.py`:

1. `_scan_relationship_annotations` stops appending to `_PENDING_RELATIONS` directly. Change its two `_PENDING_RELATIONS.append((model_name, field_name, metadata))` lines to collect into a local list and return it:

```python
        local_relations = {}
        fields_to_remove = []
        pending_relations = []
        ...
                pending_relations.append((field_name, metadata))   # M2M branch
        ...
                        pending_relations.append((field_name, metadata))   # FK branch
        ...
        return local_relations, fields_to_remove, pending_relations
```

2. In `__new__`, adapt the unpack and flush the pending entries with the identity after stamping:

```python
        local_relations, fields_to_remove, pending_relations = (
            mcs._scan_relationship_annotations(annotations, namespace, name)
        )
        ...
        cls.__ferro_identity__ = f"{cls.__module__}.{cls.__qualname__}"
        cls.__ferro_table__ = mcs._resolve_table_name(name, namespace)
        for field_name, metadata in pending_relations:
            _PENDING_RELATIONS.append((cls.__ferro_identity__, field_name, metadata))

        mcs._register_model_and_proxies(cls, cls.__ferro_identity__, local_relations)
```

3. `_register_model_and_proxies` keys by the passed identity (`_MODEL_REGISTRY_PY[name] = cls` already does — the argument is now the identity).

4. `_generate_and_register_schema` receives and uses the identity for registration and IR:

```python
        mcs._generate_and_register_schema(
            cls, cls.__ferro_identity__, ferro_fields, local_relations
        )
```

(its body already uses the `name` parameter for `register_model_schema` and `compile_model_schema_ir`; the error message `f"Ferro failed to register model '{name}'"` now names the identity — keep it).

5. In `_inject_relation_descriptors` (around :540-552), when `metadata.to` is a concrete model class, pass its identity; strings/ForwardRefs stay as-is (resolved lazily):

```python
                target_name = (
                    metadata.to.__ferro_identity__
                    if hasattr(metadata.to, "__ferro_identity__")
                    else target_name
                )
```

(place after the existing `target_name` assignment, before constructing `ForwardDescriptor`).

- [ ] **Step 3: Python — relations + descriptors resolve via `resolve_model_reference`**

`src/ferro/relations/__init__.py`:

```python
from ..state import (  # noqa: F401
    _JOIN_TABLE_REGISTRY,
    _MODEL_REGISTRY_PY,
    _PENDING_RELATIONS,
    resolve_model_reference,
)
```

First pass string resolution (:38-45):

```python
        if isinstance(rel.to, (str, ForwardRef)):
            to_name = rel.to if isinstance(rel.to, str) else rel.to.__forward_arg__
            target_model = resolve_model_reference(to_name, default=None)
            if not target_model:
                raise RuntimeError(
                    f"Relationship resolution failed: '{to_name}' not found"
                )
            rel.to = target_model
```

(ambiguity raises from inside `resolve_model_reference` with candidates listed).

Descriptor injections use identities: in the `ForeignKey` branch `target_model_name=model_name` is already the identity (pending entries carry it); in the M2M branch replace `target_model_name=target_model.__name__` with `target_model_name=target_model.__ferro_identity__`.

`src/ferro/relations/descriptors.py` — both lazy lookups:

```python
from ..state import resolve_model_reference
...
        if self._target_model is None:
            self._target_model = resolve_model_reference(self.target_model_name)
```

(in both `RelationshipDescriptor.__get__` and `ForwardDescriptor.__get__`; `resolve_model_reference` raises "Model '…' not found in registry" — drop the manual `if None: raise` blocks).

- [ ] **Step 4: Python — models.py and builder.py pass identities over the FFI**

`src/ferro/models.py` — replace the FFI-bound `self.__class__.__name__` / `cls.__name__` at: `save()` (save_record ×2, update_record, register_instance), `delete()` (`name = ...`), `refresh()` (`name = ...`), `bulk_create` (save_bulk_records), `_reregister_ferro` with `__ferro_identity__`:

```python
        # e.g. in save():
            new_id = await save_record(
                self.__class__.__ferro_identity__,
                save_bind_payload(self),
                route,
                mode="upsert",
            )
```

Human-facing messages (`ModelDoesNotExist(...)`, `"Cannot UPDATE a persisted {self.__class__.__name__}"`, `f"Model {cls.__name__} does not define a primary key"`) keep `__name__`.

Public `evict_instance` accepts a model class or a name string:

```python
def evict_instance(
    model: "type[Model] | str",
    pk: str,
    *,
    using: str | None = None,
    session: "Session | None" = None,
) -> None:
    """Remove one instance from the active scope's identity map.

    ``model`` is a model class, its qualified identity, or an unambiguous
    bare class name (ambiguity raises with the candidates listed).
    """
    from .state import resolve_model_reference

    model_cls = resolve_model_reference(model) if isinstance(model, str) else model
    route = resolve_operation_scope(using=using, session=session)
    _core_evict_instance(model_cls.__ferro_identity__, pk, route)
```

`src/ferro/query/builder.py` — the three `"model_name": self.model_cls.__name__` payload sites and the three name-string args (`count_filtered`, `update_filtered`, `delete_filtered`) become `self.model_cls.__ferro_identity__`. The `__repr__` keeps `__name__`.

`src/ferro/ir/compiler.py` — remove the dead `if model_name == "Model": continue` in `compile_registry_schema_ir` (the base class is never registered).

- [ ] **Step 5: Rust — ops extract `__ferro_identity__`**

In `src/state.rs` add:

```rust
/// Extract the qualified Ferro identity stamped on a model class (FF-E E1).
///
/// # Errors
/// `PyTypeError` when the object is not a Ferro model class.
pub fn model_identity(cls: &Bound<'_, PyAny>) -> PyResult<String> {
    let attr = cls.getattr("__ferro_identity__").map_err(|_| {
        pyo3::exceptions::PyTypeError::new_err(
            "Object is not a Ferro model class (missing __ferro_identity__)",
        )
    })?;
    attr.extract::<String>().map_err(|_| {
        pyo3::exceptions::PyTypeError::new_err("__ferro_identity__ must be a str")
    })
}
```

In `src/operations.rs`, `fetch_all` / `fetch_one` / `fetch_filtered` replace:

```rust
    let name = cls.getattr("__name__")?.extract::<String>()?;
```

with:

```rust
    let name = crate::state::model_identity(&cls)?;
```

- [ ] **Step 6: Rebuild and run everything**

Run:
```bash
uv run maturin develop
uv run pytest tests/ -q
```
Expected: PASS. Failures will cluster in tests asserting bare-name registry keys or identity-map names — fix each to use `__ferro_identity__` / class-based `evict_instance` (string form still works for unambiguous names, e.g. `evict_instance("ComplexModel", ...)` in `tests/test_structural_types.py` keeps passing unchanged).

- [ ] **Step 7: Commit**

```bash
git add -A src tests
git commit -m "feat(ff-e)!: E1 — registries key by qualified model identity on both sides

_MODEL_REGISTRY_PY and the Rust MODEL_REGISTRY now key by
module.qualname (__ferro_identity__): two same-named models in
different modules coexist instead of silently clobbering (F9). FK
string references resolve unambiguous short names and error listing
qualified candidates on ambiguity; QueryIR/SchemaIR model_name and
identity-map keys carry the identity; evict_instance accepts a model
class or name string.

BREAKING CHANGE: registry keys, identity-map keys, and IR model_name
are qualified names; code reading _MODEL_REGISTRY_PY or passing bare
class names over the FFI must adapt."
```

---

### Task 7: E1 — table-name collision detection at class definition

**Files:**
- Modify: `src/ferro/metaclass.py` (`_register_model_and_proxies` collision scan)
- Modify: `src/ferro/relations/__init__.py` (join-table vs model-table guard)
- Modify: `tests/test_models.py:20` (redesign `test_duplicate_model_registration`)
- Test: `tests/test_model_identity.py` (collision + idempotency exit gates)

**Interfaces:**
- Consumes: identity/table stamps, qualified keys, registry isolation.
- Produces: a second **distinct** model resolving to an already-claimed table name raises `RuntimeError` at class definition naming both candidates; same-identity redefinition remains idempotent (latest wins).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_model_identity.py`:

```python
def test_two_distinct_models_sharing_a_table_error_names_both():
    def make_a():
        class SharedTable(Model):
            __ferro_table__: ClassVar[str] = "ff_e_shared_table"
            id: int | None = None

        return SharedTable

    a = make_a()
    with pytest.raises(RuntimeError) as excinfo:

        class Other(Model):
            __ferro_table__: ClassVar[str] = "ff_e_shared_table"
            id: int | None = None

    message = str(excinfo.value)
    assert "ff_e_shared_table" in message
    assert a.__ferro_identity__ in message
    assert "Other" in message
    assert "__ferro_table__" in message


def test_same_identity_redefinition_is_idempotent():
    from ferro.state import _MODEL_REGISTRY_PY

    class Redefined(Model):  # noqa: F811
        id: int | None = None
        name: str

    class Redefined(Model):  # noqa: F811
        id: int | None = None
        name: str
        age: int

    assert "age" in Redefined.model_fields
    assert _MODEL_REGISTRY_PY[Redefined.__ferro_identity__] is Redefined


def test_m2m_join_table_colliding_with_model_table_errors():
    from typing import Annotated  # noqa: F401

    from ferro import ManyToMany, Relation, BackRef
    from ferro.relations import resolve_relationships

    class JoinVictim(Model):
        __ferro_table__: ClassVar[str] = "joinsource_tags"
        id: int | None = None

    class JoinSource(Model):
        id: int | None = None
        tags: Relation[list["JoinTagT"]] = ManyToMany(related_name="sources")

    class JoinTagT(Model):
        id: int | None = None
        sources: Relation[list[JoinSource]] = BackRef()

    with pytest.raises(RuntimeError, match="joinsource_tags"):
        resolve_relationships()
```

Run: `uv run pytest tests/test_model_identity.py -k 'shared_table or idempotent or join_table_colliding' -v`
Expected: collision tests FAIL (no error raised today); idempotency PASSES already (keep it — it pins the contract).

- [ ] **Step 2: Redesign `tests/test_models.py::test_duplicate_model_registration`**

Replace the existing test (it encodes F9's silent clobber as a spec) with the new contract:

```python
def test_duplicate_model_registration():
    """FF-E E1 contract: redefining the *same* model (same module + qualname,
    e.g. REPL or module re-import) is idempotent — the latest definition wins.
    Two *distinct* models resolving to the same table name is a hard error at
    class definition (see tests/test_model_identity.py)."""

    class DuplicateModel(Model):
        name: str

    class DuplicateModel(Model):  # noqa
        name: str
        age: int

    assert "age" in DuplicateModel.model_fields
```

- [ ] **Step 3: Implement the collision scan in the metaclass**

In `src/ferro/metaclass.py` `_register_model_and_proxies`:

```python
    @staticmethod
    def _register_model_and_proxies(cls, identity: str, local_relations: dict) -> None:
        """
        Register model in global registry and inject FieldProxy for query building.

        Raises:
            RuntimeError: When a *distinct* model already claims this model's
                resolved table name (FF-E E1). Re-registration under the same
                qualified identity is idempotent.
        """
        table_name = cls.__ferro_table__
        for key, other in _MODEL_REGISTRY_PY.items():
            if key == identity:
                continue
            if getattr(other, "__ferro_table__", None) == table_name:
                raise RuntimeError(
                    f"Ferro model '{identity}' resolves to table '{table_name}', "
                    f"which is already registered by model '{key}'. Two distinct "
                    "models cannot share a table. Set __ferro_table__ on one of "
                    "them to give it a distinct table name."
                )
        _MODEL_REGISTRY_PY[identity] = cls
        cls.ferro_relations = local_relations

        # Inject FieldProxy for each field to enable operator overloading on the class
        for field_name in cls.model_fields:
            setattr(cls, field_name, FieldProxy(field_name))
```

- [ ] **Step 4: Join-table guard in `resolve_relationships`**

In `src/ferro/relations/__init__.py`, right after `join_table` is resolved:

```python
            for key, other in _MODEL_REGISTRY_PY.items():
                if getattr(other, "__ferro_table__", None) == join_table:
                    raise RuntimeError(
                        f"M2M relation '{model_name}.{field_name}' derives join "
                        f"table '{join_table}', which is already the table of "
                        f"model '{key}'. Set through= on the relation or "
                        "__ferro_table__ on the model to resolve the collision."
                    )
```

- [ ] **Step 5: Run tests, then the full suite**

Run:
```bash
uv run pytest tests/test_model_identity.py tests/test_models.py -v
uv run pytest tests/ -q
```
Expected: PASS. Any suite failure is a real leaked-model collision — fix the offending test's isolation, never weaken the check.

- [ ] **Step 6: Commit**

```bash
git add src/ferro/metaclass.py src/ferro/relations/__init__.py tests/test_models.py tests/test_model_identity.py
git commit -m "feat(ff-e)!: E1 — duplicate table names error at class definition

A second distinct model resolving to an already-claimed table name now
raises at class definition, naming both candidates and the
__ferro_table__ fix; M2M join tables colliding with a model's table
error at resolution. Redefinition under the same qualified identity
(REPL, module re-import) stays idempotent — test_duplicate_model_registration
now encodes that contract explicitly.

BREAKING CHANGE: two distinct models sharing one table name was a
silent registry clobber (F9); it is now a definition-time error."
```

---

### Task 8: E3 — kill the O(N²) import cost + budget test

**Files:**
- Modify: `src/ferro/metaclass.py:575` (delete `compile_registry_schema_ir()`; drop import)
- Modify: `src/ferro/ir/compiler.py` (`compile_model_schema_ir` accepts a prebuilt schema)
- Test: `tests/test_import_budget.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `compile_model_schema_ir(model_name, model_cls, schema=None)`; class definition performs exactly one `build_model_schema` per model; the full modelset compiles only at the lazy entry points (`connect`/`create_tables`/`migrate`/`get_metadata`), which already call `compile_registry_schema_ir()`.

- [ ] **Step 1: Write the failing budget test**

Create `tests/test_import_budget.py`:

```python
"""FF-E E3 exit gate: defining N models costs O(N) schema builds, not O(N²).

Instruments build_model_schema (the expensive Pydantic-schema pass) at both
consumer modules and counts calls across a 200-model synthetic fixture. The
old per-class compile_registry_schema_ir() recompiled every registered model
on every class definition — ~N²/2 builds for N models.
"""


from ferro import Model


def test_import_cost_is_linear_in_model_count(monkeypatch):
    import ferro.ir.compiler as ir_compiler
    import ferro.metaclass as mc

    calls = {"n": 0}
    real = mc.build_model_schema

    def counting(model_cls, schema=None):
        calls["n"] += 1
        return real(model_cls, schema)

    monkeypatch.setattr(mc, "build_model_schema", counting)
    monkeypatch.setattr(ir_compiler, "build_model_schema", counting)

    n_models = 200
    for i in range(n_models):
        type(
            f"BudgetModel{i}",
            (Model,),
            {"__annotations__": {"id": int | None, "name": str}, "id": None},
        )

    # One build per class definition (the metaclass builds; the per-model IR
    # compile reuses it). 2×N headroom tolerates one extra build per class,
    # but O(N²) (~20,000 for N=200) must fail loudly.
    assert calls["n"] <= 2 * n_models, calls["n"]
```

Run: `uv run pytest tests/test_import_budget.py -v`
Expected: FAIL — call count ≈ N²/2 + 2N (each definition recompiles the whole registry).

- [ ] **Step 2: Delete the per-class registry recompile; reuse the built schema**

`src/ferro/metaclass.py` `_generate_and_register_schema`:

```python
        try:
            schema = build_model_schema(cls)

            if schema:
                setattr(cls, "__ferro_schema__", schema)
                register_model_schema(name, json.dumps(schema), cls.__ferro_table__)
                compile_model_schema_ir(name, cls, schema=schema)
        except Exception as e:
            raise RuntimeError(f"Ferro failed to register model '{name}': {e}")
```

and drop `compile_registry_schema_ir` from the `from .ir import ...` line (keep `compile_model_schema_ir`).

`src/ferro/ir/compiler.py`:

```python
def compile_model_schema_ir(
    model_name: str, model_cls: type[Any], schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Compile and persist a single model's SchemaIR envelope + fingerprint.

    Args:
        model_name: Registry key (qualified model identity).
        model_cls: Python model class to compile.
        schema: Optional prebuilt canonical schema — avoids a redundant
            build_model_schema pass when the caller just built it (FF-E E3).

    Returns:
        The compiled SchemaIR envelope for ``model_cls``.
    """
    if schema is None:
        schema = build_model_schema(model_cls)
    payload = compile_schema_ir_payload(
        model_name,
        schema,
        table_name=getattr(model_cls, "__ferro_table__", None),
    )
    envelope = wrap_schema_ir(payload)
    _SCHEMA_IR_BY_MODEL[model_name] = envelope
    _SCHEMA_IR_FINGERPRINT_BY_MODEL[model_name] = _fingerprint(envelope)
    return envelope
```

- [ ] **Step 3: Run the budget test and the IR/vector suites**

Run: `uv run pytest tests/test_import_budget.py tests/test_ir_vectors_contract.py tests/test_naming_single_source.py -v && uv run pytest tests/ -q`
Expected: PASS — the lazy entry points (`connect`/`create_tables`/`migrate`/`get_metadata`) all call `compile_registry_schema_ir()` themselves, so no consumer observes a missing modelset.

- [ ] **Step 4: Commit**

```bash
git add src/ferro/metaclass.py src/ferro/ir/compiler.py tests/test_import_budget.py
git commit -m "perf(ff-e): E3 — one schema build per class definition, O(N) import

Class definition no longer recompiles the whole registry's SchemaIR
(O(N²) across an import of N models, finding F11); the per-model IR
compile reuses the schema the metaclass just built. The full modelset
still compiles at the lazy entry points, which already push it. Adds a
200-model instrumented budget test pinning the O(N) contract."
```

---

### Task 9: Docs — `__ferro_table__` guide + roadmap tick

**Files:**
- Modify: `docs/pages/guide/models-and-fields.md` (new "Custom table names" section)
- Modify: `docs/plans/2026-07-02-001-fable-fixes-roadmap.md` (tick E1–E4 + exit gate)

- [ ] **Step 1: Document `__ferro_table__`**

In `docs/pages/guide/models-and-fields.md`, add a section (match the file's existing tab markup exactly — check a nearby example first; mkdocs-material uses `=== "Assignment"` / `=== "Annotated"`):

~~~markdown
## Custom table names

By default a model's table is its class name lowercased: `class User` →
table `user`. Set `__ferro_table__` in the class body to choose the table
name yourself:

=== "Assignment"

    ```python
    from typing import ClassVar

    from ferro import Field, Model


    class User(Model):
        __ferro_table__: ClassVar[str] = "app_users"

        id: int | None = Field(default=None, primary_key=True)
        email: str = Field(unique=True)
    ```

=== "Annotated"

    ```python
    from typing import Annotated, ClassVar

    from ferro import FerroField, Model


    class User(Model):
        __ferro_table__: ClassVar[str] = "app_users"

        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]
    ```

Both declare a `User` model stored in `app_users` — every generated
statement follows it:

```sql
CREATE TABLE "app_users" (
  "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
  "email" varchar NOT NULL
)
```

Relationships follow it too: a `ForeignKey` to `User` renders
`REFERENCES "app_users" ("id")`, and default many-to-many join tables are
named from the participants' table names.

`__ferro_table__` applies only to the class that declares it — a subclass
gets its own default (`classname.lower()`) unless it declares its own.
The name must be a 1–63 character identifier (`[A-Za-z_][A-Za-z0-9_]*`).

Two distinct models cannot share one table: defining a second model that
resolves to an already-claimed table name raises immediately at class
definition, naming both models. Redefining the *same* class (in a REPL or
re-imported module) is fine.
~~~

- [ ] **Step 2: Tick the roadmap**

In `docs/plans/2026-07-02-001-fable-fixes-roadmap.md`, flip E1–E4 checkboxes and the FF-E exit-gate box to `- [x]`.

- [ ] **Step 3: Build docs if a docs build exists; commit**

Run: `just --list 2>/dev/null | grep -i docs` and run the docs build if present; otherwise skip.

```bash
git add docs/pages/guide/models-and-fields.md docs/plans/2026-07-02-001-fable-fixes-roadmap.md
git commit -m "docs(ff-e): document __ferro_table__; tick FF-E in the roadmap"
```

---

### Task 10: Verification sweep

**Files:** none (verification only; fix regressions where found)

- [ ] **Step 1: Grep gates**

```bash
grep -n 'to_lowercase()' src/*.rs | grep -v 'direction\|introspect\|query.rs'
grep -rn '\.lower()' src/ferro/ --include='*.py'
grep -n 'except Exception' src/ferro/relations/__init__.py
```
Expected: first grep → only `schema.rs`'s `_render_create_table_sql_for_test` match-helper; second → only `ir/compiler.py:271` (single resolution point), `schema_metadata.py`'s documented provisional fallback, enum-type naming (`alembic.py`, `schema_metadata.py:159` — not table derivations), and `query/builder.py` direction parsing; third → no swallowing handler.

- [ ] **Step 2: Rust test suites**

```bash
cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate
cargo test --no-default-features --features testing
```
Expected: PASS.

- [ ] **Step 3: Full matrix + shadow-strict**

```bash
uv run maturin develop
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1 uv run pytest tests/test_migrate_plan.py tests/test_shadow_reports.py tests/test_auto_migrate.py -q
```
Expected: all green (both shadow flags — STRICT alone is a silent no-op). The matrix is green on `main`; any failure is a regression from this branch — diff behavior against `main` in a worktree before touching the test.

- [ ] **Step 4: Fix anything found, amend/commit as `fix(ff-e): …`, re-run until green.**
