# FF-F Query Builder 1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the query builder 1.0-shaped per the approved design (`docs/plans/2026-07-06-009-ff-f-query-builder-1.0-design.md`): immutable copy-on-write chaining, build-time column validation, lambda-only predicate surface (operator style / `col()` / class-attribute `FieldProxy` injection deleted), a scoped `ty` gate, and `ferro_schema_ir::QueryNode` as the only query shape in Rust.

**Architecture:** Python side first (F-1 → F-2 → F-5a order_by → F-3/F-5b removal → ty gate), Rust `QueryPlan` collapse (F-4) last since it is the widest mechanical change, docs after. Every task is TDD with the exit-gate tests written first.

**Tech Stack:** Python ≥3.13 (so `typing.Self` is available), Pydantic v2, PyO3/maturin, sea-query, `ty` (Astral) for the static gate, pytest with `--db-backends=sqlite,postgres`.

## Global Constraints

- Branch: `ff-f/query-builder-1.0` (already created off `main`; design doc committed as `ba734fb`).
- Conventional commits, standard types only, scope `ff-f`, `!` on user-observable breaking commits. **No AI attribution anywhere** (no Co-Authored-By, no "Generated with" — AGENTS.md I-6).
- Do NOT edit version fields in `pyproject.toml`/`Cargo.toml` (semantic-release computes 0.14.0).
- After every Rust change: `uv run maturin develop`. No panics across FFI: `PyResult` everywhere, no `unwrap()` on Python-facing data.
- **Never bulk-reformat.** `cargo fmt`/`ruff format` flag `main` itself; hand-format only your own hunks.
- Tests declaring models inside functions get registry isolation from the autouse `_ferro_registry_isolation` fixture in `tests/conftest.py`; keep function-local model names distinct across the module (same-named models still collide at definition).
- Postgres for matrix runs: `FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres` (docker `local-pg`). Matrix is green on `main` — any failure is a real regression.
- Docs rules: field-declaring examples show **both** Assignment + Annotated tabs (I-8); query examples use **lambda** predicates (I-9); plain-language + example-first (I-11).

---

### Task 1: F-1 — Immutable copy-on-write chaining

**Files:**
- Modify: `src/ferro/query/builder.py`
- Test: `tests/test_query_immutability.py` (create)

**Interfaces:**
- Consumes: current `Query`/`Relation` from `src/ferro/query/builder.py`.
- Produces: `Query._clone(self) -> Self` (private); `where`/`order_by`/`limit`/`offset`/`_m2m` return **new** instances typed `-> Self`; `first()` no longer touches `self._limit`. `Relation` loses its `where`/`order_by`/`limit`/`offset`/`_m2m` pass-through overrides (the `Self` return type makes them redundant); its `TYPE_CHECKING` overloads for `all`/`first` stay.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_query_immutability.py`:

```python
"""FF-F F-1 exit-gate tests: the builder never mutates in place."""

from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Model
from ferro.query import Query, Relation


pytestmark = pytest.mark.sqlite_only


class TestImmutableChaining:
    def test_where_returns_new_query_and_leaves_original_unchanged(self):
        class ImmUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q1 = Query(ImmUser)
        q2 = q1.where(lambda u: u.age >= 18)
        assert q2 is not q1
        assert q1.where_clause == []
        assert len(q2.where_clause) == 1

    def test_chained_where_does_not_alias_where_clause_list(self):
        class ImmUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0
            name: str = ""

        q1 = Query(ImmUser2).where(lambda u: u.age >= 18)
        q2 = q1.where(lambda u: u.name == "x")
        assert q1.where_clause is not q2.where_clause
        assert len(q1.where_clause) == 1
        assert len(q2.where_clause) == 2

    def test_limit_offset_order_by_do_not_mutate(self):
        class ImmUser3(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q1 = Query(ImmUser3)
        q2 = q1.limit(5)
        q3 = q2.offset(10)
        q4 = q3.order_by("age", "desc")
        assert (q1._limit, q1._offset, q1.order_by_clause) == (None, None, [])
        assert q2._limit == 5 and q2._offset is None
        assert q3._offset == 10
        assert q4.order_by_clause == [{"column": "age", "direction": "desc"}]
        assert q3.order_by_clause == []

    def test_m2m_context_is_not_shared_between_clones(self):
        class ImmUser4(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        q1 = Query(ImmUser4)._m2m("jt", "src", "tgt", 1)
        q2 = q1.limit(3)
        assert q1._m2m_context is not q2._m2m_context
        assert q2._m2m_context == q1._m2m_context

    def test_relation_chaining_preserves_relation_type(self):
        class ImmUser5(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        r = Relation(ImmUser5).where(lambda u: u.age >= 1).limit(2)
        assert isinstance(r, Relation)


class TestTerminalsDoNotMutate:
    @pytest.mark.asyncio
    async def test_first_does_not_mutate_limit(self, tmp_path):
        class FirstImm(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str = ""

        db = tmp_path / "first_imm.db"
        await ferro.connect(f"sqlite:{db}?mode=rwc", auto_migrate=True)
        async with ferro.engines.session():
            await FirstImm(id=1, name="a").save()
            await FirstImm(id=2, name="b").save()
            q = FirstImm.select()
            got = await q.first()
            assert got is not None
            assert q._limit is None          # first() must not write _limit
            assert len(await q.all()) == 2   # q still returns everything

    @pytest.mark.asyncio
    async def test_exists_does_not_mutate(self, tmp_path):
        class ExistsImm(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        db = tmp_path / "exists_imm.db"
        await ferro.connect(f"sqlite:{db}?mode=rwc", auto_migrate=True)
        async with ferro.engines.session():
            await ExistsImm(id=1).save()
            q = ExistsImm.select()
            assert await q.exists() is True
            assert q._limit is None and q._offset is None
```

Note: `test_where_returns_new_query...` uses a lambda predicate — already the supported path today, so this file needs no changes in later tasks (Task 2 adds validation, which these valid columns pass).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_query_immutability.py -x -q`
Expected: FAIL — `q2 is not q1` assertions fail (methods currently `return self`), and `q1._limit is None` fails after `first()`.

- [ ] **Step 3: Implement copy-on-write in `builder.py`**

In `src/ferro/query/builder.py`:

a. Add imports: `import copy` (top-level) and add `Self` to the `typing` import line.

b. Add `_clone` to `Query` (after `_transaction_or_using`):

```python
def _clone(self) -> Self:
    """Return a copy of this query with no shared mutable state.

    ``copy.copy`` preserves the concrete class (``Relation`` stays
    ``Relation``); the mutable containers are then replaced so chained
    queries never alias the originals (FF-F F-1).
    """
    new = copy.copy(self)
    new.where_clause = list(self.where_clause)
    new.order_by_clause = list(self.order_by_clause)
    new._m2m_context = (
        dict(self._m2m_context) if self._m2m_context is not None else None
    )
    return new
```

c. Rewrite the chain methods to clone (keep the docstrings, updating "The current Query instance for chaining" → "A new ``Query`` with the clause added; ``self`` is unchanged."):

```python
def _m2m(self, join_table, source_col, target_col, source_id) -> Self:
    new = self._clone()
    new._m2m_context = {
        "join_table": join_table,
        "source_col": source_col,
        "target_col": target_col,
        "source_id": source_id,
    }
    return new

def where(self, node: "QueryNode | Predicate[T]") -> Self:      # signature narrows in Task 4
    new = self._clone()
    new.where_clause.append(_resolve_where_node(node))
    return new

def order_by(self, field: Any, direction: str = "asc") -> Self:
    if direction.lower() not in ("asc", "desc"):
        raise ValueError("direction must be 'asc' or 'desc'")
    col_name = field.column if hasattr(field, "column") else str(field)
    new = self._clone()
    new.order_by_clause.append({"column": col_name, "direction": direction.lower()})
    return new

def limit(self, value: int) -> Self:
    new = self._clone()
    new._limit = value
    return new

def offset(self, value: int) -> Self:
    new = self._clone()
    new._offset = value
    return new
```

Keep the existing `@overload` pair on `Query.where` for now (Task 4 deletes it); change the overload return annotations from `"Query[T]"` to `Self`.

d. Rewrite `first()`:

```python
async def first(self) -> T | None:
    results = await self.limit(1).all()
    return results[0] if results else None
```

(keep the docstring, drop the try/finally and `old_limit`).

e. In `Relation`, **delete** the `_m2m`, `where` (both overloads + impl), `order_by`, `limit`, `offset` overrides entirely — `Self` on the base class now types them correctly. Keep the `TYPE_CHECKING` `all`/`first` overloads, the runtime `all`/`first` wrappers, and `__get_pydantic_core_schema__`.

- [ ] **Step 4: Run the new tests, then the query/relation suites**

Run: `uv run pytest tests/test_query_immutability.py -q`
Expected: PASS.

Run: `uv run pytest tests/test_query_builder.py tests/test_query_typing.py tests/test_relationships.py tests/test_transactions.py -q`
Expected: PASS. (`tests/test_query_builder.py:139` `test_query_chaining_placeholders` chains `where(...).limit(10).offset(5)` off the returned values, so it survives immutability. If any test fails because it relied on aliasing — e.g. calling `q.limit(5)` without using the return value — fix the *test* to use the returned query and note it in the commit body as the F-1 behavior change.)

- [ ] **Step 5: Commit**

```bash
git add src/ferro/query/builder.py tests/test_query_immutability.py
git commit -m "feat(ff-f)!: immutable copy-on-write query chaining

where/order_by/limit/offset/_m2m return new Query instances; first()
no longer temporarily mutates _limit. Code relying on chained calls
mutating the receiver must use the returned query.

Closes F-1 of the FF-F epic (F12)."
```

---

### Task 2: F-2 — Build-time column validation in `QueryProxy`

**Files:**
- Modify: `src/ferro/metaclass.py` (compute `__ferro_query_columns__`)
- Modify: `src/ferro/query/nodes.py` (`validate_query_column`, `QueryProxy`)
- Modify: `src/ferro/query/builder.py` (`_resolve_where_node` threads `model_cls`)
- Test: `tests/test_query_column_validation.py` (create)

**Interfaces:**
- Consumes: `Query.model_cls`; `local_relations` in `ModelMetaclass._register_model_and_proxies`.
- Produces:
  - `model_cls.__ferro_query_columns__: frozenset[str]` — set at class registration: `frozenset(model_fields) | {f"{name}_id" for FK relations}`.
  - `ferro.query.nodes.validate_query_column(model_cls: type, name: str) -> str` — raises `AttributeError` on a miss.
  - `QueryProxy.__init__(self, model_cls: type)` — **required** arg; `__getattr__` validates then returns `FieldProxy(name)`.
  - `_resolve_where_node(node: Any, model_cls: type) -> QueryNode` — new second parameter.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_query_column_validation.py`:

```python
"""FF-F F-2 exit-gate tests: misspelled columns fail at build time."""

from typing import Annotated

import pytest

from ferro import FerroField, ForeignKey, Model
from ferro.query import Query, QueryProxy


pytestmark = pytest.mark.sqlite_only


class TestWhereColumnValidation:
    def test_misspelled_column_raises_at_build_time(self):
        class ValUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str = ""

        with pytest.raises(AttributeError) as exc:
            ValUser.where(lambda u: u.nmae == "x")
        message = str(exc.value)
        assert "nmae" in message
        assert "name" in message        # did-you-mean + valid columns list
        assert "id" in message

    def test_valid_columns_pass(self):
        class ValUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str = ""

        q = ValUser2.where(lambda u: u.name == "x")
        assert q.where_clause[0].column == "name"

    def test_shadow_fk_column_is_valid(self):
        class ValAuthor(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        class ValPost(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author: Annotated[ValAuthor, ForeignKey(related_name="val_posts")]

        q = Query(ValPost).where(lambda p: p.author_id == 1)
        assert q.where_clause[0].column == "author_id"

    def test_error_lists_shadow_fk_in_valid_columns(self):
        class ValAuthor2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        class ValPost2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            author: Annotated[ValAuthor2, ForeignKey(related_name="val_posts2")]

        with pytest.raises(AttributeError, match="author_id"):
            Query(ValPost2).where(lambda p: p.authr_id == 1)


class TestQueryProxyContract:
    def test_query_proxy_requires_model(self):
        with pytest.raises(TypeError):
            QueryProxy()  # model_cls is required — no unvalidated mode

    def test_query_proxy_validates_directly(self):
        class ValUser3(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        proxy = QueryProxy(ValUser3)
        assert proxy.id.column == "id"
        with pytest.raises(AttributeError, match="Valid columns"):
            _ = proxy.bogus
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_query_column_validation.py -x -q`
Expected: FAIL — `QueryProxy()` currently accepts no args and validates nothing.

- [ ] **Step 3: Implement**

a. `src/ferro/metaclass.py` — in `_register_model_and_proxies` (currently ~`:406`), after `cls.ferro_relations = local_relations` add:

```python
        # Queryable-column set for build-time predicate validation (FF-F F-2):
        # declared fields plus the shadow {fk}_id columns.
        shadow_fk_columns = {
            f"{field_name}_id"
            for field_name, metadata in local_relations.items()
            if isinstance(metadata, ForeignKey)
        }
        cls.__ferro_query_columns__ = frozenset(cls.model_fields) | shadow_fk_columns
```

(`ForeignKey` is already imported in `metaclass.py`.)

b. `src/ferro/query/nodes.py` — add `import difflib` at the top, then above `QueryProxy`:

```python
def validate_query_column(model_cls: type, name: str) -> str:
    """Validate a queryable column name at build time (FF-F F-2).

    Raises:
        AttributeError: If ``name`` is not a declared field or shadow
            ``{fk}_id`` column of ``model_cls``. The message names the bad
            column, suggests the closest valid one, and lists all valid
            columns.
    """
    valid = getattr(model_cls, "__ferro_query_columns__", None)
    if valid is None:
        raise TypeError(
            f"{model_cls!r} is not a registered Ferro model class; "
            "query predicates require a Ferro Model."
        )
    if name in valid:
        return name
    close = difflib.get_close_matches(name, sorted(valid), n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    raise AttributeError(
        f"{model_cls.__name__} has no queryable column {name!r}.{hint} "
        f"Valid columns: {', '.join(sorted(valid))}."
    )
```

c. Replace `QueryProxy` in `nodes.py`:

```python
class QueryProxy(Generic[TModel]):
    """Validating attribute proxy passed to lambda predicates (FF-F F-2).

    A fresh ``QueryProxy`` is constructed for the queried model each time a
    lambda predicate is evaluated. Attribute access validates the name
    against the model's queryable columns (declared fields plus shadow
    ``{fk}_id`` columns) and returns a :class:`FieldProxy` — so
    ``lambda user: user.archived == False`` builds a :class:`QueryNode`,
    while ``lambda user: user.archievd == False`` raises ``AttributeError``
    at build time naming the valid columns.

    Attribute types are ``FieldProxy[Any]``: per-field static types for bare
    lambda parameters require TypeScript-style mapped types, proposed for
    Python in PEP 827 (draft, targeting 3.16) — adopted here when type
    checkers support it.

    Examples:
        >>> rows = await User.where(lambda user: user.archived == False).all()  # noqa: E712
    """

    __slots__ = ("_model_cls",)

    def __init__(self, model_cls: type) -> None:
        self._model_cls = model_cls

    def __getattr__(self, name: str) -> "FieldProxy[Any]":
        """Validate ``name`` and return a ``FieldProxy`` for it."""
        validate_query_column(self._model_cls, name)
        return FieldProxy(name)
```

d. `src/ferro/query/builder.py` — thread the model into resolution:

```python
def _resolve_where_node(node: Any, model_cls: type) -> QueryNode:
```

and inside, change `result = node(QueryProxy())` to `result = node(QueryProxy(model_cls))`. Update the call site in `Query.where`: `new.where_clause.append(_resolve_where_node(node, self.model_cls))`.

- [ ] **Step 4: Run the new tests plus the touched suites**

Run: `uv run pytest tests/test_query_column_validation.py -q`
Expected: PASS.

Run: `uv run pytest tests/test_query_typing.py tests/test_query_builder.py tests/test_query_immutability.py tests/test_framework_predicates.py tests/test_relationships.py -q`
Expected: one pre-existing test now fails by design — `tests/test_query_typing.py:150` `test_query_proxy_attribute_returns_field_proxy` constructs `QueryProxy()` bare. Update it in place:

```python
    def test_query_proxy_attribute_returns_field_proxy(self):
        """QueryProxy attribute access yields a validated FieldProxy at runtime."""

        class ProxyUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            archived: bool = False

        proxy = QueryProxy(ProxyUser)
        f = proxy.archived
        assert isinstance(f, FieldProxy)
        assert f.column == "archived"
```

Also update the `TYPE_CHECKING` static block at `tests/test_query_typing.py:300`: `QueryProxy[_StaticUser]()` → `QueryProxy[_StaticUser](_StaticUser)`.

Everything else must pass — framework helpers (`_field_eq` in `models.py` uses `getattr(t, field_name)`, which routes through the same validation with real field names).

- [ ] **Step 5: Commit**

```bash
git add src/ferro/metaclass.py src/ferro/query/nodes.py src/ferro/query/builder.py \
        tests/test_query_column_validation.py tests/test_query_typing.py
git commit -m "feat(ff-f)!: validate predicate columns at build time

QueryProxy now requires the queried model and validates attribute
access against model_fields plus shadow {fk}_id columns; a misspelled
column raises AttributeError naming the valid columns instead of
silently building a junk predicate.

Closes F-2 of the FF-F epic (F12)."
```

---

### Task 3: F-5 (order_by) — lambda-or-string surface, validated

**Files:**
- Modify: `src/ferro/query/builder.py` (`order_by`)
- Test: extend `tests/test_query_column_validation.py`

**Interfaces:**
- Consumes: `validate_query_column` and `QueryProxy` from Task 2; `_clone` from Task 1.
- Produces: `Query.order_by(field: "str | Callable[[QueryProxy[T]], FieldProxy[Any]]", direction: str = "asc") -> Self`. Lambda receives a validating `QueryProxy` and must return a `FieldProxy`; strings are validated against the same column set. The `field.column`-attribute duck-typing path (`order_by(User.name)`) is **removed** (dies with injection in Task 4 anyway).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_query_column_validation.py`:

```python
class TestOrderByValidation:
    def test_order_by_lambda_is_validated_and_extracts_column(self):
        class ObUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            created_at: str = ""

        q = ObUser.select().order_by(lambda u: u.created_at, "desc")
        assert q.order_by_clause == [{"column": "created_at", "direction": "desc"}]

    def test_order_by_string_is_validated(self):
        class ObUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        q = ObUser2.select().order_by("age")
        assert q.order_by_clause == [{"column": "age", "direction": "asc"}]

    def test_order_by_misspelled_string_raises(self):
        class ObUser3(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        with pytest.raises(AttributeError, match="age"):
            ObUser3.select().order_by("aeg")

    def test_order_by_misspelled_lambda_raises(self):
        class ObUser4(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        with pytest.raises(AttributeError, match="Valid columns"):
            ObUser4.select().order_by(lambda u: u.aeg)

    def test_order_by_lambda_must_return_field_proxy(self):
        class ObUser5(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        with pytest.raises(TypeError, match="FieldProxy"):
            ObUser5.select().order_by(lambda u: u.age >= 3)

    def test_order_by_rejects_bad_direction(self):
        class ObUser6(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        with pytest.raises(ValueError, match="asc"):
            ObUser6.select().order_by("id", "sideways")
```

(Reuses the module's existing imports; model names stay distinct per the registry-collision rule.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_query_column_validation.py::TestOrderByValidation -x -q`
Expected: FAIL — lambda path currently stringifies the callable (`str(field)`), misspelled strings pass silently.

- [ ] **Step 3: Implement the 1.0 `order_by`**

Replace `Query.order_by` in `src/ferro/query/builder.py` (imports: add `FieldProxy` and `validate_query_column` to the `from .nodes import ...` line; add `Callable` to typing imports):

```python
def order_by(
    self,
    field: "str | Callable[[QueryProxy[T]], FieldProxy[Any]]",
    direction: str = "asc",
) -> Self:
    """Add an ordering clause and return a new query.

    Accepts a lambda naming the column (``order_by(lambda u: u.created_at,
    "desc")`` — the documented style, matching ``where`` predicates) or a
    column-name string (``order_by("created_at", "desc")``). Both forms are
    validated against the model's queryable columns at build time.

    Args:
        field: Column selector — lambda receiving a :class:`QueryProxy`,
            or a column-name string.
        direction: ``"asc"`` (default) or ``"desc"``.

    Returns:
        A new ``Query`` with the ordering added; ``self`` is unchanged.

    Raises:
        AttributeError: If the column is not a queryable column.
        TypeError: If a lambda selector returns something other than a
            single column reference.
        ValueError: If ``direction`` is not ``"asc"`` or ``"desc"``.

    Examples:
        >>> newest = await Post.select().order_by(lambda p: p.created_at, "desc").all()
    """
    if direction.lower() not in ("asc", "desc"):
        raise ValueError("direction must be 'asc' or 'desc'")

    if callable(field):
        selected = field(QueryProxy(self.model_cls))
        if not isinstance(selected, FieldProxy):
            raise TypeError(
                "order_by() selector must return a single column "
                f"(e.g. `lambda u: u.created_at`), got {type(selected).__name__}"
            )
        col_name = selected.column
    elif isinstance(field, str):
        col_name = validate_query_column(self.model_cls, field)
    else:
        raise TypeError(
            "order_by() expected a column-name string or a lambda selector, "
            f"got {type(field).__name__}"
        )

    new = self._clone()
    new.order_by_clause.append({"column": col_name, "direction": direction.lower()})
    return new
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_query_column_validation.py tests/test_query_immutability.py -q`
Expected: PASS. (`test_query_immutability.py` uses `order_by("age", "desc")` — the string path.)

Do **not** run the full suite yet: `order_by(Model.field)` call sites across `tests/test_aggregation.py`, `tests/test_documentation_features.py`, `tests/test_transactions.py`, `tests/test_connection.py` now fail — they are migrated in Task 4 together with the injection removal (splitting the migration would churn the same files twice).

- [ ] **Step 5: Commit**

```bash
git add src/ferro/query/builder.py tests/test_query_column_validation.py
git commit -m "feat(ff-f)!: order_by takes a validated lambda or column string

order_by(lambda u: u.created_at, 'desc') is the 1.0 form (consistent
with where predicates); order_by('created_at') is validated shorthand.
The order_by(Model.field) class-attribute form is removed. Misspelled
columns raise AttributeError at build time, deleting the documented
junk-column trap.

Part of F-5 of the FF-F epic (F12)."
```

---

### Task 4: F-3/F-5 — delete injection, operator style, `col()`, and AST deprecation state; migrate tests

**Files:**
- Modify: `src/ferro/metaclass.py:429-431,582-583` (delete both `FieldProxy` injections)
- Modify: `src/ferro/query/nodes.py` (delete `col`, `predicate_style`, `uses_operator_style`, `to_dict`)
- Modify: `src/ferro/query/builder.py` (predicate-only `where`, delete deprecation shim)
- Modify: `src/ferro/query/__init__.py`, `src/ferro/models.py`, `pyproject.toml`
- Delete: `tests/test_deprecated_operator_inventory.py`, `tests/test_framework_predicates.py`
- Modify: `tests/test_query_builder.py`, `tests/test_query_typing.py`, `tests/test_aggregation.py`, `tests/test_documentation_features.py`, `tests/test_transactions.py`, `tests/test_connection.py` (+ any other `order_by(Model.field)`/operator/`col()` sites the greps below surface)

**Interfaces:**
- Consumes: validating `QueryProxy` (Task 2), new `order_by` (Task 3).
- Produces: `Query.where(self, predicate: "Predicate[T]") -> Self` — predicate callables **only** (no `QueryNode`, no overloads). `Model.where(cls, predicate: "Predicate[Self]", *, session=None) -> Query[Self]` and `ModelConnection.where(self, predicate: "Predicate[M]") -> Query[M]` likewise single-signature. `FieldProxy.__init__(self, column: str)` (no `predicate_style`). `QueryNode.__init__` loses `predicate_style`. `ferro.query.__all__` loses `"col"`. `User.age` at class level raises `AttributeError` (normal Pydantic v2 semantics).

- [ ] **Step 1: Write the failing removal tests**

Append to `tests/test_query_column_validation.py`:

```python
class TestOperatorSurfaceRemoved:
    def test_class_attribute_is_not_a_field_proxy(self):
        class RmUser(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            age: int = 0

        with pytest.raises(AttributeError):
            RmUser.age  # normal Pydantic v2 class-attribute semantics restored

    def test_col_is_gone(self):
        with pytest.raises(ImportError):
            from ferro.query import col  # noqa: F401

    def test_where_rejects_raw_query_node(self):
        from ferro.query import QueryNode

        class RmUser2(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        with pytest.raises(TypeError, match="predicate callable"):
            RmUser2.where(QueryNode("id", "==", 1))
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_query_column_validation.py::TestOperatorSurfaceRemoved -x -q`
Expected: FAIL — `RmUser.age` is a `FieldProxy`, `col` imports, raw nodes accepted.

- [ ] **Step 3: Delete the runtime surfaces**

a. `src/ferro/metaclass.py`:
- In `_register_model_and_proxies`, delete the injection loop (`# Inject FieldProxy for each field...` + the `for field_name in cls.model_fields: setattr(...)` two lines). Rename the method `_register_model` and update its docstring + both call/reference sites (`grep -n '_register_model_and_proxies' src/ferro/metaclass.py`).
- In `_inject_relation_descriptors`, delete the two shadow-injection lines `id_field_name = f"{field_name}_id"` / `setattr(cls, id_field_name, FieldProxy(id_field_name))` (the `{fk}_id` **name** still reaches `__ferro_query_columns__` via Task 2's computation from `local_relations`).
- Remove the now-unused `FieldProxy` import if nothing else in the file uses it (`grep -n FieldProxy src/ferro/metaclass.py`).

b. `src/ferro/query/nodes.py`:
- Delete `col()` entirely.
- `FieldProxy.__init__(self, column: str)` — drop `predicate_style`; drop `predicate_style=...` from every `QueryNode(...)` construction in the operator methods; update the class docstring (it references `User.email == ...` operator examples — replace with lambda examples).
- `QueryNode.__init__` — drop the `predicate_style` parameter and attribute; delete `uses_operator_style`; in `__or__`/`__and__` drop the `predicate_style=` argument blocks.
- Delete `QueryNode.to_dict` (its only consumers are its own recursion and one test updated below; `to_ir_dict` is the wire format).

c. `src/ferro/query/builder.py`:
- Delete the `_deprecated_operator_query_node` function and the whole `from .._deprecations import (...)` block (check `IR_FIRST_MIGRATION_GUIDE_PREDICATES` has no other consumers: `grep -rn IR_FIRST_MIGRATION_GUIDE_PREDICATES src/`; if `_deprecations.py` still defines it unused, leave `_deprecations.py` itself untouched — other constants are consumed elsewhere).
- Replace `_resolve_where_node` (predicate-only):

```python
def _resolve_where_node(predicate: "Predicate[Any]", model_cls: type) -> QueryNode:
    """Evaluate a lambda predicate against a validating ``QueryProxy``."""
    if not callable(predicate):
        raise TypeError(
            "where() expected a predicate callable "
            f"(e.g. `lambda user: user.age >= 18`), got {type(predicate).__name__}"
        )
    result = predicate(QueryProxy(model_cls))
    if not isinstance(result, QueryNode):
        raise TypeError(
            "where() predicate callable must return QueryNode, "
            f"got {type(result).__name__}"
        )
    return result
```

- `Query.where`: delete both `@overload`s; single signature `def where(self, predicate: "Predicate[T]") -> Self:`; rewrite the docstring lambda-first with no operator/`col()` mentions (note the build-time column validation); keep the clone-append body from Task 1.
- Module docstring + imports: `Predicate` moves from the `TYPE_CHECKING` block to a real import (`from .nodes import FieldProxy, Predicate, QueryNode, QueryProxy, _serialize_query_value, validate_query_column`).

d. `src/ferro/query/__init__.py` — remove `col` from the import and `__all__`.

e. `src/ferro/models.py`:
- `Model.where` (~`:514`): delete the two `@overload`s; single signature `def where(cls, predicate: "Predicate[Self]", *, session: "Session | None" = None) -> Query[Self]:`; docstring lambda-only; body unchanged (`return Query(cls, session=session).where(predicate)`).
- `ModelConnection.where` (~`:754`): same collapse to `def where(self, predicate: "Predicate[M]") -> Query[M]:`.
- Remove `QueryNode` from the `from .query import ...` line if now unused (`grep -n QueryNode src/ferro/models.py`).

f. `pyproject.toml:84` — delete the `deprecated_operator_path` marker line.

- [ ] **Step 4: Migrate the test suite**

a. Delete files whose subject no longer exists:

```bash
git rm tests/test_deprecated_operator_inventory.py tests/test_framework_predicates.py
```

(the former inventories operator-marked tests; the latter asserts framework helpers emit no operator-deprecation warnings — the warning no longer exists.)

b. `tests/test_query_builder.py`:
- `test_field_proxy_operator_overloading` (`:98`): keep — build the proxy directly: `expr = FieldProxy("age") >= 18` (FieldProxy operators are the lambda mechanism, still core).
- `test_model_where_clause` (`:119`): drop the `deprecated_call` wrapper and marker; predicate becomes `AgeUser.where(lambda u: u.age >= 21)` (keep the model the test already defines; assertions unchanged).
- `test_query_chaining_placeholders` (`:139`): same conversion to a lambda; drop marker.
- `test_col_style_where_does_not_emit_deprecation_warning` (`:180`): delete (subject removed).
- `test_query_node_to_dict_serializes_uuid_values_inside_in_filters` (`:80`): convert to `to_ir_dict()`: build the same node, assert `node.to_ir_dict()["value"]["value"] == [str(uid1), str(uid2)]`.
- Remove `col` and any `pytest.mark.deprecated_operator_path` imports/usages; sweep the rest of the file for operator-style predicates (`grep -n 'deprecated_call\|col(' tests/test_query_builder.py` must return nothing when done).

c. `tests/test_query_typing.py`:
- Delete `class TestColWrapper`, `class TestOperatorPathUnchanged`, `class TestCombinedStyles` and the `col` import.
- In the `TYPE_CHECKING` block delete the two `col(...)` `assert_type` lines; keep (from Task 2) the `Predicate`/`QueryProxy` assertions and add one for the FieldProxy mechanism:

```python
    # FieldProxy comparisons resolve to QueryNode (the lambda mechanism)
    assert_type(FieldProxy("archived") == False, QueryNode)  # noqa: E712
```

- Update the module docstring ("the three predicate styles" → lambda predicates only).

d. Mechanical `order_by(Model.field)` → string/lambda migration (string for terse test call sites is fine; docs use lambda):

```bash
grep -rn 'order_by([A-Z][A-Za-z]*\.' tests/
```

Expected sites (verify with the grep — line numbers drift): `tests/test_aggregation.py:54,62,70,71`, `tests/test_transactions.py:106`, `tests/test_connection.py:220`, `tests/test_documentation_features.py:466,472,485,489`. Rewrite each e.g. `order_by(AggProduct.price)` → `order_by("price")`, `order_by(DocUser.username, "desc")` → `order_by("username", "desc")`.

e. Sweep for any remaining operator-style predicates or injected-proxy reliance:

```bash
grep -rn 'where([A-Z][A-Za-z]*\.' tests/
grep -rn 'deprecated_operator_path\|from ferro.query import.*col\|query import col' tests/ src/
```

Fix every hit (operator predicates → lambda). Both greps must come back empty.

- [ ] **Step 5: Run the full sqlite suite**

Run: `uv run pytest -q -x --db-backends=sqlite`
Expected: PASS. Failure triage: instance attribute access on shadow FK columns (`inst.author_id`) must still work — hydration sets instance attributes, and instances never hit the deleted class-level proxies; if any test proves otherwise, stop and re-examine (that would be a real regression of the removal, not a test to patch).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(ff-f)!: lambda-only predicates; remove operator style, col(), FieldProxy injection

The scheduled v0.14.0 removal: class attributes are no longer
FieldProxy (normal Pydantic semantics restored), col() is deleted,
where() accepts only lambda predicates, and predicate_style
deprecation bookkeeping leaves the query AST.

Closes F-3/F-5 of the FF-F epic (F12)."
```

---

### Task 5: Scoped `ty` gate + static contract tests

**Files:**
- Modify: `pyproject.toml` (dev dep), `justfile` (`check` target), `.github/workflows/ci.yml` (gate step)
- Modify: `src/ferro/query/builder.py` (fix the 6 `__ferro_identity__` diagnostics)
- Create: `tests/static_fixtures/good_predicates.py`, `tests/static_fixtures/bad_predicates.py`
- Test: `tests/test_static_contracts.py` (extend)

**Interfaces:**
- Consumes: the post-Task-4 query surface.
- Produces: `just check` running `uv run ty check src/ferro/query tests/test_query_typing.py tests/test_static_contracts.py`; a CI step in `test-python-pr` (after `maturin develop`, before pytest — the lint job has no built `_core`, so the gate lives where the extension exists); subprocess-driven static-contract tests that fail if the checker stops biting; the Rust grep-gate test.

- [ ] **Step 1: Write the failing static-contract tests**

Append to `tests/test_static_contracts.py`:

```python
import json
import subprocess
import sys

FIXTURES = Path(__file__).parent / "static_fixtures"


def _run_ty(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "uv", "run", "ty", "check", "--output-format", "concise", str(target)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )


def test_valid_lambda_predicates_pass_the_type_checker():
    result = _run_ty(FIXTURES / "good_predicates.py")
    assert result.returncode == 0, (
        f"good predicates must type-check cleanly:\n{result.stdout}\n{result.stderr}"
    )


def test_junk_lambda_predicates_fail_the_type_checker():
    """`lambda t: True` (bool, not QueryNode) must be rejected statically.

    Bare-lambda RHS typing (`u.age >= "x"`) is NOT checkable today: it
    requires TypeScript-style mapped types (PEP 827, draft, targeting
    Python 3.16). When checkers support it, QueryProxy's attribute typing
    upgrades from FieldProxy[Any] with zero runtime change.
    """
    result = _run_ty(FIXTURES / "bad_predicates.py")
    assert result.returncode != 0, "bad_predicates.py must fail ty"
    assert "invalid-assignment" in result.stdout


def test_query_ir_is_the_only_query_shape_in_rust():
    """FF-F F-4 exit gate: no `struct QueryNode` outside the IR crate."""
    repo = Path(__file__).parent.parent
    offenders = []
    for rust_file in (repo / "src").rglob("*.rs"):
        if "struct QueryNode" in rust_file.read_text(encoding="utf-8"):
            offenders.append(str(rust_file))
    assert offenders == [], (
        f"legacy QueryNode shape resurfaced outside crates/ferro-schema-ir: {offenders}"
    )


def test_operator_predicate_surface_is_gone():
    nodes_src = Path("src/ferro/query/nodes.py").read_text(encoding="utf-8")
    assert "def col(" not in nodes_src
    assert "predicate_style" not in nodes_src
    builder_src = Path("src/ferro/query/builder.py").read_text(encoding="utf-8")
    assert "_deprecated_operator_query_node" not in builder_src
    metaclass_src = Path("src/ferro/metaclass.py").read_text(encoding="utf-8")
    assert "FieldProxy(" not in metaclass_src
```

(`json` import only if unused elsewhere — drop it; `Path` is already imported at the top of the file.)

Create `tests/static_fixtures/good_predicates.py`:

```python
"""Type-checked (never executed): valid lambda predicates resolve to QueryNode."""

from typing import Annotated, assert_type

from ferro import FerroField, Model
from ferro.query import FieldProxy, Predicate, QueryNode, QueryProxy


class GoodUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    age: int = 0
    archived: bool = False


pred_compare: Predicate[GoodUser] = lambda u: u.age >= 18
pred_compound: Predicate[GoodUser] = lambda u: (u.age >= 18) & (u.archived == False)  # noqa: E712
pred_in: Predicate[GoodUser] = lambda u: u.age.in_([1, 2, 3])

assert_type(pred_compare(QueryProxy[GoodUser](GoodUser)), QueryNode)
assert_type(FieldProxy("age") >= 18, QueryNode)
```

Create `tests/static_fixtures/bad_predicates.py`:

```python
"""Type-checked (never executed): junk predicates must FAIL `ty check`.

test_static_contracts.py asserts this file produces `invalid-assignment`
diagnostics — if it starts passing, the static gate has stopped biting.
"""

from typing import Annotated

from ferro import FerroField, Model
from ferro.query import Predicate


class BadUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    age: int = 0


bad_bool: Predicate[BadUser] = lambda u: True          # bool is not QueryNode
bad_value: Predicate[BadUser] = lambda u: u.age        # FieldProxy is not QueryNode
```

- [ ] **Step 2: Run to verify current state**

Run: `uv run pytest tests/test_static_contracts.py -x -q`
Expected: FAIL — `ty` is not a project dependency yet (`uv run ty` errors), and `src/ferro/query` still has 6 diagnostics.

- [ ] **Step 3: Wire the gate**

a. `uv add --group dev ty` (locks a reproducible version).

b. Fix the 6 `builder.py` diagnostics (`type[T@Query]` has no attribute `__ferro_identity__`) with one helper + one suppression instead of six:

```python
def _model_identity(model_cls: type) -> str:
    """Qualified registry identity of a Ferro model class (FF-E)."""
    return model_cls.__ferro_identity__  # ty: ignore[unresolved-attribute]
```

Replace all six `self.model_cls.__ferro_identity__` reads in `builder.py` with `_model_identity(self.model_cls)` (`grep -n __ferro_identity__ src/ferro/query/builder.py` must show only the helper).

c. `justfile` — add after `test`:

```make
check:
    uv run ty check src/ferro/query tests/test_query_typing.py tests/test_static_contracts.py
```

d. `.github/workflows/ci.yml` — in the `test-python-pr` job, insert between `uv run maturin develop` (`:153`) and the IR-vectors pytest step (`:157`):

```yaml
      - name: Static type gate (ty, scoped)
        run: |
          uv run ty check src/ferro/query tests/test_query_typing.py tests/test_static_contracts.py
```

- [ ] **Step 4: Run the gate and the tests**

Run: `just check`
Expected: `All checks passed!`

Run: `uv run pytest tests/test_static_contracts.py -q`
Expected: the two `ty` subprocess tests and `test_operator_predicate_surface_is_gone` PASS; `test_query_ir_is_the_only_query_shape_in_rust` **FAILS** (legacy `struct QueryNode` still in `src/query.rs` until Task 6). Mark it expected-fail *temporarily is not allowed* — instead run only the passing subset now and note the Rust gate goes green in Task 6:

Run: `uv run pytest tests/test_static_contracts.py -q --deselect tests/test_static_contracts.py::test_query_ir_is_the_only_query_shape_in_rust`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock justfile .github/workflows/ci.yml \
        src/ferro/query/builder.py tests/test_static_contracts.py tests/static_fixtures/
git commit -m "test(ff-f): scoped ty gate over the query surface

Adds ty as a dev dependency, a 'just check' target and CI step over
src/ferro/query + the typed-predicate tests, subprocess static-contract
tests that fail if the checker stops biting, and the Rust
QueryNode-only-in-IR-crate grep gate (goes green with the F-4 collapse).

Part of F-3 of the FF-F epic (F12)."
```

---

### Task 6: F-4 — Rust `QueryPlan` collapse onto the IR types

**Files:**
- Modify: `src/query.rs` (delete legacy shapes; add `QueryPlan`)
- Modify: `src/operations.rs` (consume `QueryPlan`; delete query shadow machinery)
- Modify: `src/lib.rs:129` (unregister `_shadow_compare_query_plan_for_test`)
- Modify: `src/ferro/_core.pyi:107` (drop the stub)
- Modify: `tests/test_shadow_reports.py` (migration-only)

This is the widest mechanical change — give it to the strongest model. The compiler drives the call-site migration; the interfaces below are the contract.

**Interfaces:**
- Consumes: `ferro_schema_ir::{IrEnvelope, QueryIrPayload, QueryNode, QueryOrderBy, QueryValue}` (`crates/ferro-schema-ir/src/lib.rs:139-197`); `crate::state::{MODEL_REGISTRY, RegisteredModel, Dialect}`; `crate::codec::query_bind_expr`.
- Produces in `src/query.rs`:

```rust
/// Runtime query plan: canonical IR payload plus per-query runtime state.
/// The runtime fields are NOT IR and are never serialized (FF-F F-4).
pub struct QueryPlan {
    pub model_name: String,
    pub where_clause: Vec<ferro_schema_ir::QueryNode>,
    pub order_by: Vec<ferro_schema_ir::QueryOrderBy>,   // empty = no ORDER BY
    pub limit: Option<u64>,
    pub offset: Option<u64>,
    pub m2m: Option<M2mContext>,
    pub postgres_enum_udt: std::collections::HashMap<String, String>,
    pub registration: Option<std::sync::Arc<crate::state::RegisteredModel>>,
}

impl QueryPlan {
    pub fn from_ir_payload(payload: QueryIrPayload) -> Result<QueryPlan, String>;
    pub fn to_condition_for_backend(&self, backend: Dialect) -> Result<Condition, String>;
    pub fn value_rhs_simple_expr_for_backend(&self, col_name: &str, val: &Value,
        infer_uuid_without_schema: bool, backend: Dialect) -> SimpleExpr;
}
```

`M2mContext` (unchanged shape) stays in `query.rs`. **Deleted:** legacy `struct QueryNode`, `struct OrderBy`, `struct QueryDef`, `QuerySemanticSignature`, `semantic_signature`, `to_ir_payload`, `query_node_to_ir`, `query_node_from_ir`, `query_def_from_ir_payload`, `query_node_semantic_string`, `query_value_semantic_string`, `query_value_kind`.

- [ ] **Step 1: Write the failing Rust test**

Add to the `tests` module in `src/query.rs` (this replaces the legacy-shape tests below):

```rust
#[test]
fn query_plan_builds_from_ir_payload_and_lowers_null_eq_to_is_null() {
    let payload: ferro_schema_ir::QueryIrPayload = serde_json::from_value(serde_json::json!({
        "model_name": "Pending",
        "where": [
            {"node_kind": "leaf", "column": "attached_at", "operator": "==",
             "value": {"kind": "null", "value": null}},
            {"node_kind": "compound", "operator": "OR",
             "left": {"node_kind": "leaf", "column": "age", "operator": ">=",
                      "value": {"kind": "int", "value": 18}},
             "right": {"node_kind": "leaf", "column": "name", "operator": "LIKE",
                       "value": {"kind": "string", "value": "a%"}}}
        ],
        "order_by": [{"column": "age", "direction": "desc"}],
        "limit": 10, "offset": 5, "m2m": null
    }))
    .expect("payload deserializes");
    let plan = super::QueryPlan::from_ir_payload(payload).expect("plan builds");
    assert_eq!(plan.order_by.len(), 1);
    assert_eq!(plan.limit, Some(10));

    let mut select = Query::select();
    select.from(Alias::new("pending")).cond_where(
        plan.to_condition_for_backend(Dialect::Sqlite).expect("valid"),
    );
    let sql = select.to_string(SqliteQueryBuilder).to_lowercase();
    assert!(sql.contains("is null"), "IR null eq must lower to IS NULL: {sql}");
    assert!(!sql.contains("= null"), "must not emit = NULL: {sql}");
    assert!(sql.contains("or"), "compound OR preserved: {sql}");
}
```

Run: `cargo test --no-default-features --features testing query_plan_builds -- --nocapture`
Expected: FAIL — `QueryPlan` does not exist.

- [ ] **Step 2: Implement `QueryPlan` in `src/query.rs`**

a. `from_ir_payload` (mirrors the deleted converter, minus the node translation):

```rust
pub fn from_ir_payload(payload: QueryIrPayload) -> Result<QueryPlan, String> {
    let m2m: Option<M2mContext> = match payload.m2m {
        Some(value) => serde_json::from_value(value)
            .map(Some)
            .map_err(|e| format!("invalid QueryIR m2m payload: {e}"))?,
        None => None,
    };
    let registration = crate::state::MODEL_REGISTRY
        .read()
        .ok()
        .and_then(|registry| registry.get(&payload.model_name).cloned());
    Ok(QueryPlan {
        model_name: payload.model_name,
        where_clause: payload.where_clause,
        order_by: payload.order_by,
        limit: payload.limit,
        offset: payload.offset,
        m2m,
        postgres_enum_udt: std::collections::HashMap::new(),
        registration,
    })
}
```

b. Port `to_condition_for_backend`/`node_to_condition_for_backend` to walk `ferro_schema_ir::QueryNode`. Shape of the walker (the operator lowering arms — `==`→`is_null`/`eq`, `!=`, `<`, `<=`, `>`, `>=`, `IN` array/scalar, `LIKE`, catch-all `eq` — move over verbatim from the current `QueryDef` impl):

```rust
fn node_to_condition_for_backend(
    &self,
    node: &ferro_schema_ir::QueryNode,
    backend: Dialect,
) -> Result<Condition, String> {
    match node {
        ferro_schema_ir::QueryNode::Compound { operator, left, right } => {
            let left_cond = self.node_to_condition_for_backend(left, backend)?;
            let right_cond = self.node_to_condition_for_backend(right, backend)?;
            Ok(match operator.as_str() {
                "OR" => Condition::any().add(left_cond).add(right_cond),
                "AND" => Condition::all().add(left_cond).add(right_cond),
                op => return Err(format!("unsupported compound QueryNode operator: {op}")),
            })
        }
        ferro_schema_ir::QueryNode::Leaf { operator, column, value } => {
            let col = Expr::col(Alias::new(column));
            // IR always carries a value object; JSON null arrives as
            // QueryValue { kind: "null", value: Value::Null }.
            let rhs_is_json_null = value.value.is_null();
            // ... identical operator arms to the current QueryDef walker,
            // reading `value.value` where the legacy walker read
            // `node.value.as_ref().unwrap_or(&Value::Null)` — the Option
            // dance disappears because IR values are total.
            todo!("port the operator arms verbatim")
        }
    }
}
```

(The `todo!` is for this plan document only — the implementer ports the ~100 lines of operator arms from the current `impl QueryDef` before deleting it; no `todo!` may be committed.)

c. `value_rhs_simple_expr_for_backend` — body unchanged (delegates to `crate::codec::query_bind_expr` with `self.registration`/`self.postgres_enum_udt`).

d. Delete the legacy items listed in **Interfaces → Deleted**, and delete the legacy-shape tests in the `tests` module: `json_null_deserializes_to_option_none_for_query_node_value`, `where_rhs_none_emits_is_null_for_eq_sqlite`, `where_rhs_none_emits_is_not_null_for_ne_sqlite`, `query_ir_roundtrip_preserves_semantics_signature`. Port `empty_query_def` → `empty_query_plan` (same fields, IR types) so the typed-bind tests (`uuid_rhs_*`, `null_rhs_*`, `binary_rhs_*`, `enum_rhs_*`, `decimal_rhs_*`) keep compiling unchanged.

- [ ] **Step 3: Migrate `src/operations.rs`**

a. `use crate::query::{QueryPlan};` replaces `{QueryDef, query_def_from_ir_payload}`.
b. `query_def_from_ir_json` (`:219`) → `query_plan_from_ir_json(query_ir_json: &str) -> PyResult<QueryPlan>` — same envelope kind/version checks, tail becomes `QueryPlan::from_ir_payload(envelope.payload).map_err(...)`. Rename all callers (compiler-driven); `query_condition_for_backend` and `reject_pagination_on_mutation` take `&QueryPlan`.
c. `order_by` consumers: `QueryDef.order_by` was `Option<Vec<OrderBy>>`; `QueryPlan.order_by` is `Vec<QueryOrderBy>` — `if let Some(ref orders)` blocks become `for order in &plan.order_by`.
d. **Delete the query shadow machinery** (design: comparator is vacuous with one shape, deleted not kept as tautology): `QueryPlanArtifact`, `bind_semantics`, `query_plan_artifact`, `shadow_artifact_from_ir_roundtrip`, `compare_shadow_query_artifacts`, `maybe_compare_shadow_query_artifacts` (`:494-573`) and its four call sites (`:1760`, `:1931`, `:2131`, `:2215`); the whole `_shadow_compare_query_plan_for_test` pyfunction (`:2840-2940` region, including the legacy-JSON fallback branch at `:2869-2891`). Remove its registration in `src/lib.rs:129` and its stub in `src/ferro/_core.pyi:107`. The migration shadow comparator (`_shadow_compare_migration_plan_for_test`) is untouched.
e. Preserve the FF-E loud error: the `registration.as_ref().ok_or_else(... "Model '…' not found")` pattern must remain at every point the old code had it (grep `"not found"` in `operations.rs` before/after — same count minus the deleted pyfunction).

- [ ] **Step 4: Build, run Rust tests, exit-gate grep**

```bash
cargo test --no-default-features --features testing
cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate
grep -rn 'struct QueryNode' src/ crates/
```

Expected: tests PASS; grep returns **only** `crates/ferro-schema-ir/src/lib.rs`. Also `grep -rn 'QueryDef\|query_def' src/` returns nothing.

- [ ] **Step 5: Rebuild the extension, update the shadow-report test, run Python suites**

```bash
uv run maturin develop
```

`tests/test_shadow_reports.py`: remove the `_shadow_compare_query_plan_for_test` import (`:12`) and every test/fixture block that calls it (`:65` region); migration-plan cases stay. If the stable-fixture file the CI job checks embeds query-compare output, regenerate it per that test's fixture-update instructions (read the test header).

```bash
uv run pytest tests/test_static_contracts.py -q          # Rust grep gate now green
uv run pytest -q --db-backends=sqlite
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres uv run pytest -q -m "backend_matrix or postgres_only" --db-backends=sqlite,postgres
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/query.rs src/operations.rs src/lib.rs src/ferro/_core.pyi tests/test_shadow_reports.py
git commit -m "refactor(ff-f): collapse QueryDef onto ferro_schema_ir::QueryNode

QueryPlan is a thin runtime wrapper (registration + postgres_enum_udt)
around the canonical QueryIR payload; the legacy QueryNode/QueryDef
shadow shapes, IR<->legacy converters, legacy JSON fallback, and the
now-vacuous query-path shadow comparator are deleted. QueryIR is the
only query shape in Rust.

Closes F-4 of the FF-F epic (F12)."
```

(No `!`: no user-observable Python surface changes; `_shadow_compare_query_plan_for_test` is a private `_`-prefixed test hook.)

---

### Task 7: Docs — the 1.0 query surface

**Files:**
- Modify: `docs/pages/guide/queries.md`, `docs/pages/concepts/query-typing.md`, `docs/pages/concepts/type-safety.md`, `docs/pages/api/queries.md`, `docs/pages/getting-started/quickstart.md`, `docs/pages/guide/relationships.md`, `docs/pages/howto/migrating-to-v0-12-0.md`
- Modify: `docs/examples/predicates.py`, `docs/examples/pagination.py`, `docs/examples/quickstart.py`, `docs/examples/relationships.py`

**Interfaces:** consumes the final Task 1–6 surface; produces docs that teach only the 1.0 forms.

- [ ] **Step 1: Migrate the executable examples first (they are tested)**

`grep -rn 'col(\|order_by([A-Z]\|where([A-Z][A-Za-z]*\.' docs/examples/` and rewrite every hit:
- `docs/examples/predicates.py:73-74`: `order_by(User.age, "desc")` → `order_by(lambda u: u.age, "desc")`; `order_by(User.id)` → `order_by(lambda u: u.id)`; any `col()`/operator predicate → lambda.
- `docs/examples/pagination.py:17,28`: `order_by(Article.id)` → `order_by(lambda a: a.id)`.
- `docs/examples/quickstart.py:62`: `order_by(Post.created_at, "desc")` → `order_by(lambda p: p.created_at, "desc")`.
- `docs/examples/relationships.py:89`: `order_by(Player.name)` → `order_by(lambda p: p.name)`.

Run: `uv run pytest tests/test_docs_examples.py -q` — Expected: PASS.

- [ ] **Step 2: Rewrite the prose pages**

- `docs/pages/guide/queries.md`: document immutable chaining up front (each chain call returns a new query; reusing a base query is now safe — show `base = User.where(...); page1 = base.limit(10); page2 = base.limit(10).offset(10)`); replace the `:114` order_by paragraph ("not a predicate… typed Any") with the lambda + string forms; **delete the junk-column warning** (`grep -rn 'junk' docs/` must return nothing); show the build-time `AttributeError` for a misspelled column with the actual error text (run it and paste). Plain-language + example-first (I-11); any snippet declaring model fields keeps both Assignment/Annotated tabs (I-8).
- `docs/pages/concepts/query-typing.md` + `docs/pages/concepts/type-safety.md`: remove `col()`/operator sections; state the two guarantees plainly — valid predicates type-check as `QueryNode`, junk predicates fail the checker; bare-lambda RHS typing (`u.age >= "x"`) needs mapped types, proposed in [PEP 827](https://peps.python.org/pep-0827/) (draft, Python 3.16 target), adopted with zero runtime change when checkers support it.
- `docs/pages/api/queries.md`: drop the `col` entry (11-line file — regenerate member list).
- `docs/pages/getting-started/quickstart.md` + `docs/pages/guide/relationships.md`: update any operator/`order_by(Model.field)` snippets found by `grep -rn 'order_by([A-Z]\|col(' docs/pages/`.
- `docs/pages/howto/migrating-to-v0-12-0.md`: add a short "v0.14.0: removals landed" note at the top of the predicates section — operator style and `col()` are gone; `where` takes lambdas; `order_by` takes lambda/string.

- [ ] **Step 3: Verify docs greps and commit**

```bash
grep -rn 'col(' docs/pages/ docs/examples/        # only prose mentions of the removal may remain
grep -rn 'junk' docs/
uv run pytest tests/test_docs_examples.py tests/test_documentation_features.py -q
git add docs/
git commit -m "docs(ff-f): document the 1.0 query builder surface

Immutable chaining, lambda/string order_by, build-time column errors;
remove col()/operator styles and the order_by junk-column warning;
note PEP 827 as the path to bare-lambda RHS typing."
```

---

### Task 8: Full verification + roadmap tick

**Files:**
- Modify: `docs/plans/2026-07-02-001-fable-fixes-roadmap.md:330-363`

- [ ] **Step 1: Full matrix + shadow-strict + Rust + gate**

```bash
uv run maturin develop
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1 \
  FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
cargo test --no-default-features --features testing
cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate
just check
grep -rn 'struct QueryNode' src/ crates/
```

Expected: everything green; grep → IR crate only. Both shadow flags together (STRICT alone is a silent no-op). Any matrix failure is a real regression — diff against `main` in a worktree, do not paper over.

- [ ] **Step 2: Tick the roadmap**

In `docs/plans/2026-07-02-001-fable-fixes-roadmap.md`: mark F-1…F-5 `[x]`; mark the FF-F exit-gate box `[x]`; edit the F-6 bullet to `**F-6 — spun off**: aggregations + partial selects moved to their own epic on the post-F shape (tracked separately; see the FF-F design doc §7).`

- [ ] **Step 3: Commit**

```bash
git add docs/plans/2026-07-02-001-fable-fixes-roadmap.md
git commit -m "docs(ff-f): tick F-1..F-5 and exit gate; note F-6 spin-off"
```

---

### Task 9: Tracking + PR (FF-D/FF-E convention)

**Files:** none (GitHub state).

- [ ] **Step 1: Milestone, epic, native sub-issues on Project #7 (owner `syn54x`)**

Template: FF-E epic #210 + subs #211–#214, PR #209. Create milestone `FF-F`; epic issue "Epic FF-F — Query builder 1.0 shape"; sub-issues for F-1…F-5; attach each as a native sub-issue:

```bash
gh api repos/syn54x/ferro-orm/issues/<epic>/sub_issues -F sub_issue_id=<node .id>
```

Add all to Project #7, milestone FF-F. (Push account note below applies to `gh` too — confirm `gh auth status` shows `0x054`.)

- [ ] **Step 2: Push and open the PR**

Push via the `0x054` inline-token URL (osxkeychain serves the wrong credential):

```bash
TOKEN=$(gh auth token)
git push https://0x054:$TOKEN@github.com/syn54x/ferro-orm.git ff-f/query-builder-1.0
```

PR leads with before/after proof, each demonstrated concretely (show-don't-tell):
1. Aliasing fix: the `q2 = q1.where(...)` before/after snippet.
2. Misspelled column: paste the actual `AttributeError` message.
3. Failing static check: the `bad_predicates.py` diagnostic output.
4. The `grep -rn 'struct QueryNode' src/ crates/` output (IR crate only).
5. Plainly stated user-observable breaks: immutable chaining, operator style + `col()` + `order_by(Model.field)` + `where(QueryNode)` removed.

No AI attribution in the PR body. Set every project item **Done** (Project #7 auto-closes on Done — set Done first, skip manual close) once merged.

---

## Self-Review Notes

- **Spec coverage:** F-1 → Task 1; F-2 → Task 2; F-5 order_by → Task 3; F-3/F-5 removals → Tasks 4–5; F-4 → Task 6; docs → Task 7; exit gates → Tasks 1, 2, 5 (static + grep), 8 (matrix/shadow); F-6 spin-off → Task 8 roadmap edit; tracking → Task 9.
- **Deliberate deviation from the design doc:** `semantic_signature` is *deleted* rather than "reimplemented over IR nodes" — its only consumers are the query shadow artifacts the design itself deletes as vacuous (verified: `query_plan_artifact` chain + the `_shadow_compare_query_plan_for_test` hook + one round-trip unit test). If a future consumer needs semantic fingerprints, it is derivable from IR nodes then.
- **Known suite-wide risk points:** instance attribute reads of shadow `{fk}_id` after injection removal (Task 4 Step 5 triage note), and the shadow-reports stable fixture (Task 6 Step 5).
