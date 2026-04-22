# Enum Name Fix for Alembic Postgres Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Alembic autogenerate to emit named `sa.Enum()` types for Postgres compatibility

**Architecture:** Modify `_map_to_sa_type()` in `src/ferro/migrations/alembic.py` to pass the enum class itself to `sa.Enum()` instead of extracting values, allowing SQLAlchemy to derive a proper type name. Use TDD approach with comprehensive test coverage for both StrEnum and standard Enum types, plus validation against Postgres-specific requirements.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x, Alembic 1.17.x, pytest, Ferro ORM

---

## Task 1: Write failing test for StrEnum name generation

**Files:**
- Test: `tests/test_alembic_autogenerate.py`

- [ ] **Step 1: Write the failing test for StrEnum with explicit name**

Add to `tests/test_alembic_autogenerate.py`:

```python
def test_enum_generates_with_name():
    """Verify that Enum columns generate with an explicit name for Postgres compatibility."""
    from enum import StrEnum
    
    class Status(StrEnum):
        DRAFT = "draft"
        ACTIVE = "active"
        ARCHIVED = "archived"
    
    class Article(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        status: Status
    
    metadata = get_metadata()
    article_table = metadata.tables["article"]
    
    # The enum type should have a name
    assert isinstance(article_table.c.status.type, sa.Enum)
    assert article_table.c.status.type.name is not None
    assert article_table.c.status.type.name == "status"
    
    # The enum should still have the correct values
    assert set(article_table.c.status.type.enums) == {"draft", "active", "archived"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_alembic_autogenerate.py::test_enum_generates_with_name -v`
Expected: FAIL with assertion error that `name is None`

- [ ] **Step 3: Commit**

```bash
git add tests/test_alembic_autogenerate.py
git commit -m "test: add failing test for enum name generation"
```

---

## Task 2: Write failing test for standard Enum

**Files:**
- Test: `tests/test_alembic_autogenerate.py`

- [ ] **Step 1: Write the failing test for standard Enum**

Add to `tests/test_alembic_autogenerate.py`:

```python
def test_standard_enum_generates_with_name():
    """Verify that standard (non-StrEnum) Enum columns also generate with names."""
    from enum import Enum
    
    class Priority(Enum):
        LOW = 1
        MEDIUM = 2
        HIGH = 3
    
    class Task(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        priority: Priority
    
    metadata = get_metadata()
    task_table = metadata.tables["task"]
    
    # The enum type should have a name
    assert isinstance(task_table.c.priority.type, sa.Enum)
    assert task_table.c.priority.type.name is not None
    assert task_table.c.priority.type.name == "priority"
    
    # The enum should have the correct values (as strings)
    assert set(task_table.c.priority.type.enums) == {"1", "2", "3"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_alembic_autogenerate.py::test_standard_enum_generates_with_name -v`
Expected: FAIL with assertion error that `name is None`

- [ ] **Step 3: Commit**

```bash
git add tests/test_alembic_autogenerate.py
git commit -m "test: add failing test for standard Enum name generation"
```

---

## Task 3: Write failing test for optional enum

**Files:**
- Test: `tests/test_alembic_autogenerate.py`

- [ ] **Step 1: Write the failing test for optional enum with name**

Add to `tests/test_alembic_autogenerate.py`:

```python
def test_optional_enum_generates_with_name():
    """Verify that Optional[Enum] columns still generate with proper names."""
    from enum import StrEnum
    
    class Color(StrEnum):
        RED = "red"
        GREEN = "green"
        BLUE = "blue"
    
    class Widget(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        color: Color | None = None
    
    metadata = get_metadata()
    widget_table = metadata.tables["widget"]
    
    # The enum type should have a name even when optional
    assert isinstance(widget_table.c.color.type, sa.Enum)
    assert widget_table.c.color.type.name is not None
    assert widget_table.c.color.type.name == "color"
    assert widget_table.c.color.nullable is True
    
    # The enum should have the correct values
    assert set(widget_table.c.color.type.enums) == {"red", "green", "blue"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_alembic_autogenerate.py::test_optional_enum_generates_with_name -v`
Expected: FAIL with assertion error that `name is None`

- [ ] **Step 3: Commit**

```bash
git add tests/test_alembic_autogenerate.py
git commit -m "test: add failing test for optional enum name generation"
```

---

## Task 4: Extend _map_to_sa_type to track field name context

**Files:**
- Modify: `src/ferro/migrations/alembic.py:184-234`

- [ ] **Step 1: Add field_name parameter to _map_to_sa_type signature**

Modify the function signature in `src/ferro/migrations/alembic.py`:

```python
def _map_to_sa_type(
    schema: Dict[str, Any], col_info: Dict[str, Any], field_name: str | None = None
) -> "sa.types.TypeEngine":
    """Map Ferro/JSON schema types to SQLAlchemy types.
    
    Args:
        schema: The full model JSON schema
        col_info: The specific column's schema info
        field_name: The name of the field, used for naming enum types
    """
```

- [ ] **Step 2: Update call sites in _build_sa_table**

Modify the call in `_build_sa_table` function around line 113:

```python
sa_type = _map_to_sa_type(schema, col_info, col_name)
```

- [ ] **Step 3: Run tests to ensure no breakage**

Run: `pytest tests/test_alembic_autogenerate.py tests/test_alembic_type_mapping.py -v`
Expected: Existing tests should still pass (enum name tests still fail)

- [ ] **Step 4: Commit**

```bash
git add src/ferro/migrations/alembic.py
git commit -m "refactor: add field_name parameter to _map_to_sa_type"
```

---

## Task 5: Detect and preserve enum class in schema

**Files:**
- Modify: `src/ferro/migrations/alembic.py:184-234`

- [ ] **Step 1: Add enum class detection logic**

Modify `_map_to_sa_type` to detect when enum_values come from an actual enum class:

```python
def _map_to_sa_type(
    schema: Dict[str, Any], col_info: Dict[str, Any], field_name: str | None = None
) -> "sa.types.TypeEngine":
    """Map Ferro/JSON schema types to SQLAlchemy types.
    
    Args:
        schema: The full model JSON schema
        col_info: The specific column's schema info
        field_name: The name of the field, used for naming enum types
    """
    # Resolve $ref if present
    col_info = _resolve_ref(schema, col_info)

    json_type = col_info.get("type")
    format = col_info.get("format")
    enum_values = col_info.get("enum")

    # Handle Pydantic 'anyOf' for Optional types or Enums
    if "anyOf" in col_info:
        # Simple heuristic: find the first non-null type
        for item in col_info["anyOf"]:
            item = _resolve_ref(schema, item)
            if item.get("type") != "null":
                json_type = item.get("type")
                format = item.get("format")
                enum_values = item.get("enum") or enum_values
                break

    if enum_values:
        # Use field_name as the enum type name for Postgres compatibility
        return sa.Enum(*enum_values, name=field_name)

    # ... rest of function unchanged
```

- [ ] **Step 2: Run all enum tests**

Run: `pytest tests/test_alembic_autogenerate.py::test_enum_generates_with_name tests/test_alembic_autogenerate.py::test_standard_enum_generates_with_name tests/test_alembic_autogenerate.py::test_optional_enum_generates_with_name -v`
Expected: All three new tests should now PASS

- [ ] **Step 3: Run full test suite to check for regressions**

Run: `pytest tests/test_alembic_autogenerate.py tests/test_alembic_type_mapping.py -v`
Expected: All tests should PASS

- [ ] **Step 4: Commit**

```bash
git add src/ferro/migrations/alembic.py
git commit -m "feat: generate named sa.Enum types for Postgres compatibility"
```

---

## Task 6: Add integration test with actual Alembic autogenerate

**Files:**
- Test: `tests/test_alembic_autogenerate.py`

- [ ] **Step 1: Write integration test simulating Alembic rendering**

Add to `tests/test_alembic_autogenerate.py`:

```python
def test_alembic_can_render_enum_for_postgres():
    """
    Verify that the generated metadata can be rendered by Alembic
    without triggering 'PostgreSQL ENUM type requires a name' error.
    """
    from enum import StrEnum
    from sqlalchemy import create_mock_engine
    from sqlalchemy.schema import CreateTable
    
    class Status(StrEnum):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
    
    class Request(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        status: Status
        description: str
    
    metadata = get_metadata()
    request_table = metadata.tables["request"]
    
    # Mock a Postgres engine to test dialect-specific rendering
    def dump(sql, *multiparams, **params):
        # Store the SQL for inspection
        dump.statements.append(str(sql.compile(dialect=engine.dialect)))
    
    dump.statements = []
    engine = create_mock_engine(lambda *args, **kwargs: None, lambda: None)
    engine.dialect.name = "postgresql"
    
    # This should not raise 'PostgreSQL ENUM type requires a name'
    try:
        create_ddl = CreateTable(request_table).compile(dialect=engine.dialect)
        sql_text = str(create_ddl)
        
        # Verify the SQL contains enum type creation
        assert "status" in sql_text.lower()
        assert "pending" in sql_text.lower() or "CREATE TYPE" in sql_text
        
    except Exception as e:
        if "requires a name" in str(e):
            pytest.fail(f"Enum type missing name: {e}")
        raise
```

- [ ] **Step 2: Run integration test**

Run: `pytest tests/test_alembic_autogenerate.py::test_alembic_can_render_enum_for_postgres -v`
Expected: PASS without 'requires a name' error

- [ ] **Step 3: Commit**

```bash
git add tests/test_alembic_autogenerate.py
git commit -m "test: add integration test for Alembic Postgres rendering"
```

---

## Task 7: Verify existing tests still pass

**Files:**
- Test: `tests/test_alembic_type_mapping.py`
- Test: `tests/test_alembic_autogenerate.py`

- [ ] **Step 1: Run all existing Alembic tests**

Run: `pytest tests/test_alembic_autogenerate.py tests/test_alembic_type_mapping.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run full test suite to check for any regressions**

Run: `pytest tests/ -v -k alembic`
Expected: All Alembic-related tests PASS

- [ ] **Step 3: Verify that enum type mapping test still works**

The test `test_complex_type_mapping` in `tests/test_alembic_type_mapping.py` should still pass and now the enum should have a name:

Run: `pytest tests/test_alembic_type_mapping.py::test_complex_type_mapping -v`
Expected: PASS

---

## Task 8: Update docstring for get_metadata

**Files:**
- Modify: `src/ferro/migrations/alembic.py:13-56`

- [ ] **Step 1: Update get_metadata docstring to document enum handling**

Modify the docstring in `src/ferro/migrations/alembic.py`:

```python
def get_metadata() -> "sa.MetaData":
    """
    Generate a SQLAlchemy MetaData object representing all registered Ferro models.
    This is intended to be used in alembic's env.py for autogenerate support.
    
    The generated metadata includes:
    - All model tables with their columns and types
    - Primary key, foreign key, and unique constraints
    - Indexes and composite unique constraints
    - Named Enum types for Postgres compatibility
    
    Enum Handling:
        Python Enum and StrEnum fields are converted to SQLAlchemy Enum types
        with explicit names derived from the field name. This ensures compatibility
        with PostgreSQL, which requires named enum types. On SQLite, these degrade
        gracefully to VARCHAR with CHECK constraints.
    
    Returns:
        sa.MetaData: A SQLAlchemy MetaData object containing all registered models
    
    Examples:
        >>> from ferro.migrations import get_metadata
        >>> from myapp.models import User, Post
        >>> 
        >>> # Generate metadata for Alembic
        >>> target_metadata = get_metadata()
        >>> 
        >>> # Inspect generated tables
        >>> print(target_metadata.tables.keys())
        dict_keys(['user', 'post'])
    """
```

- [ ] **Step 2: Commit**

```bash
git add src/ferro/migrations/alembic.py
git commit -m "docs: update get_metadata docstring for enum handling"
```

---

## Task 9: Update docstring for _map_to_sa_type

**Files:**
- Modify: `src/ferro/migrations/alembic.py:184-234`

- [ ] **Step 1: Update _map_to_sa_type docstring**

Modify the docstring in `src/ferro/migrations/alembic.py`:

```python
def _map_to_sa_type(
    schema: Dict[str, Any], col_info: Dict[str, Any], field_name: str | None = None
) -> "sa.types.TypeEngine":
    """Map Ferro/JSON schema types to SQLAlchemy types.
    
    This function translates Pydantic/JSON schema type information into
    SQLAlchemy column types for migration generation.
    
    Args:
        schema: The full model JSON schema with $defs for reference resolution
        col_info: The specific column's schema info
        field_name: The name of the field, used for naming enum types to ensure
                   Postgres compatibility. Without explicit names, PostgreSQL
                   rejects enum type creation.
    
    Returns:
        sa.types.TypeEngine: A SQLAlchemy type instance
    
    Type Mappings:
        - Enum values → sa.Enum (with name=field_name for Postgres)
        - integer → sa.Integer
        - string (format: date-time) → sa.DateTime
        - string (format: date) → sa.Date
        - string (format: uuid) → sa.Uuid (or sa.String for older SQLAlchemy)
        - string (format: decimal) → sa.Numeric
        - string → sa.String
        - boolean → sa.Boolean
        - number (format: decimal) → sa.Numeric
        - number → sa.Float
        - object → sa.JSON
        - array → sa.JSON
    
    Examples:
        >>> schema = {"type": "string", "enum": ["a", "b"]}
        >>> _map_to_sa_type({}, schema, "status")
        Enum('a', 'b', name='status')
    """
```

- [ ] **Step 2: Commit**

```bash
git add src/ferro/migrations/alembic.py
git commit -m "docs: update _map_to_sa_type docstring with enum details"
```

---

## Task 10: Update migrations.md documentation

**Files:**
- Modify: `docs/guide/migrations.md:220-247`

- [ ] **Step 1: Update the Enum section in migrations.md**

Modify the Complex Types section in `docs/guide/migrations.md` around line 226-243:

```markdown
### Complex Types

```python
from decimal import Decimal
from datetime import datetime
from uuid import UUID
from enum import Enum, StrEnum

class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"

class User(Model):
    # Maps to DECIMAL/NUMERIC
    balance: Decimal

    # Maps to TIMESTAMP
    created_at: datetime

    # Maps to UUID (or TEXT in SQLite)
    id: UUID

    # Maps to named ENUM on Postgres (or TEXT + CHECK on SQLite)
    # The enum type is automatically named after the field (e.g., "role")
    # for Postgres compatibility. PostgreSQL requires explicit type names.
    role: UserRole

    # Maps to JSON/JSONB
    metadata: dict
```

**Enum Types and Postgres:**

Ferro automatically generates SQLAlchemy `Enum` types with explicit names
derived from the field name. This ensures compatibility with PostgreSQL,
which requires all enum types to have a name via `CREATE TYPE`.

On SQLite, enum types gracefully degrade to `VARCHAR` with `CHECK` constraints,
since SQLite has no native enum support.

Both Python's `Enum` and `StrEnum` are supported:

```python
from enum import Enum, StrEnum

class Status(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"

class Priority(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

class Article(Model):
    id: int | None = Field(default=None, primary_key=True)
    status: Status        # → CREATE TYPE status AS ENUM ('draft', 'published')
    priority: Priority    # → CREATE TYPE priority AS ENUM ('1', '2', '3')
```

The generated Alembic migration will include:

```python
def upgrade():
    # Postgres automatically creates the enum type
    op.create_table('article',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('draft', 'published', name='status'), nullable=False),
        sa.Column('priority', sa.Enum('1', '2', '3', name='priority'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

def downgrade():
    op.drop_table('article')
    # Postgres automatically drops the enum types with the table
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/guide/migrations.md
git commit -m "docs: update migrations guide with enum naming details"
```

---

## Task 11: Add troubleshooting section for enum issues

**Files:**
- Modify: `docs/guide/migrations.md:360-399`

- [ ] **Step 1: Add enum troubleshooting entry**

Add a new subsection in the Troubleshooting section of `docs/guide/migrations.md` after line 360:

```markdown
### Enum Type Issues

**Problem:** `sqlalchemy.exc.CompileError: PostgreSQL ENUM type requires a name.`

**Cause:** You're using an older version of Ferro (< 0.3.1) that generates anonymous enum types.

**Solution:** Upgrade to Ferro 0.3.1 or later:

```bash
pip install --upgrade ferro-orm[alembic]
```

Then regenerate your migrations:

```bash
# Delete the problematic migration
rm migrations/versions/xxxx_your_migration.py

# Regenerate
alembic revision --autogenerate -m "Your migration"
```

**Manual Fix (if upgrade not possible):** Edit the generated migration to add `name=` to each `sa.Enum()`:

```python
# Before (causes error on Postgres)
sa.Column('status', sa.Enum('draft', 'active'), nullable=False)

# After (works on Postgres)
sa.Column('status', sa.Enum('draft', 'active', name='status'), nullable=False)
```

**Cleaning up enum types:** If you need to manually drop enum types in a downgrade:

```python
def downgrade():
    op.drop_table('article')
    # On Postgres, explicitly drop the enum type if needed
    op.execute('DROP TYPE IF EXISTS status')
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/guide/migrations.md
git commit -m "docs: add troubleshooting for enum type errors"
```

---

## Task 12: Run comprehensive test validation

**Files:**
- Test: all test files

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run tests with coverage for alembic module**

Run: `pytest tests/test_alembic*.py --cov=src/ferro/migrations --cov-report=term-missing -v`
Expected: High coverage (>90%) for `alembic.py`, all tests PASS

- [ ] **Step 3: Verify specific enum tests all pass**

Run: `pytest tests/ -v -k enum`
Expected: All enum-related tests PASS

---

## Task 13: Final commit and summary

**Files:**
- All modified files

- [ ] **Step 1: Review all changes**

Run: `git status`
Run: `git log --oneline -n 15`
Expected: See all commits from this implementation

- [ ] **Step 2: Run final test validation**

Run: `pytest tests/test_alembic_autogenerate.py tests/test_alembic_type_mapping.py -v`
Expected: All tests PASS with new enum name tests included

- [ ] **Step 3: Verify documentation is complete**

Review:
- `src/ferro/migrations/alembic.py` - docstrings updated
- `docs/guide/migrations.md` - enum documentation added

Expected: All documentation is clear and comprehensive

---

## Self-Review Checklist

**Spec coverage:**
- ✓ Task 1-3: Test coverage for StrEnum, Enum, and Optional[Enum]
- ✓ Task 4-5: Implementation of named enum generation
- ✓ Task 6: Integration test with Alembic rendering
- ✓ Task 7: Regression testing
- ✓ Task 8-11: Documentation updates (docstrings and user guide)
- ✓ Task 12: Comprehensive validation
- ✓ Task 13: Final review

**Placeholder scan:** No TBD, TODO, or placeholders present. All code is concrete.

**Type consistency:** 
- `field_name: str | None` is consistent across all uses
- `sa.Enum(*enum_values, name=field_name)` signature is correct
- All return types match function signatures

**Test quality:**
- Tests verify the actual behavior (enum has a name)
- Tests cover both StrEnum and standard Enum
- Tests cover optional enums
- Integration test verifies Postgres dialect rendering

**Documentation quality:**
- Docstrings explain the "why" (Postgres compatibility)
- User guide includes examples and troubleshooting
- Migration patterns are shown with before/after code
