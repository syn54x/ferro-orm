"""Row security declared on Ferro models.

A model says once who is allowed to see its rows:

```python {test=compile reason="a model fragment; its imports are left out"}
class Invoice(Model):
    id: int | None = Field(default=None, primary_key=True)
    tenant_id: UUID
    amount: float

    __ferro_rls__: ClassVar = RowSecurity(
        RowPolicy(column="tenant_id", setting="app.tenant_id")
    )
```

and Ferro creates the table with row-level security switched on and the
matching policy:

```sql
ALTER TABLE "invoice" ENABLE ROW LEVEL SECURITY
ALTER TABLE "invoice" FORCE ROW LEVEL SECURITY
CREATE POLICY "rls_invoice_tenant_id" ON "invoice" FOR ALL
  USING ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
```

From then on the database decides which rows a query can see: a connection
whose `app.tenant_id` setting is unset sees no rows, and one that carries a
tenant id sees only that tenant's rows.

This module owns the declaration surface and the validation that runs at
class definition. Every SQL decision (the policy name, the `NULLIF`
expression, the `CREATE POLICY` statement) is made in the Rust core, so
every emitter renders the same bytes (AGENTS.md I-1).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._core import _ddl_row_policy_name, _rls_command_matrix, _rls_shorthand_cast

FERRO_RLS = "__ferro_rls__"
"""The class attribute a model declares its row security in."""

#: Which clauses Postgres accepts per command, read once from the shared
#: lowering layer. There is no second copy of this table here on purpose: the
#: renderer filters clauses with the same decision, so a command added in
#: ``ferro-ddl-lowering`` cannot drift out of the validation below (I-1).
_COMMAND_CLAUSES: dict[str, dict[str, bool]] = {
    row["command"]: row for row in json.loads(_rls_command_matrix())
}

COMMANDS = tuple(_COMMAND_CLAUSES)
"""The commands a policy may cover (`FOR <command>`)."""

#: A custom Postgres setting key: dotted identifiers, nothing else. Ferro's
#: renderer already doubles single quotes when it inlines the key, but that
#: escape is only sound while ``standard_conforming_strings`` is on — a server
#: with it off reads backslashes in the literal. Constraining the key to
#: identifier characters at class definition means no quote or backslash can
#: reach the rendered policy in the first place; the doubling stays as defense
#: in depth.
_SETTING_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+\Z")
_SETTING_KEY_SHAPE = "[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z0-9_]+)+"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\Z")
_NAME_SHAPE = "[a-z][a-z0-9_]*"


@dataclass(frozen=True)
class RowPolicy:
    """One Postgres row policy: which rows a command may read or write.

    It comes in two forms. The shorthand, `RowPolicy(column=..., setting=...)`,
    compares a column with a session setting and renders, for both `USING`
    and `WITH CHECK`:

    ```sql
    "tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    ```

    The cast follows the column's type: `uuid`, `text`, `varchar` and the
    integer types are supported, and any other type is refused when the
    class is defined. The `NULLIF` makes an unset or reset setting match no
    rows instead of raising a cast error; see
    [Policy expressions](../concepts/row-level-security.md#policy-expressions).
    The raw form, `RowPolicy(name=..., using=...)`, takes the SQL expressions
    as written and needs a name.

    Args:
        name: The end of the policy's name, `rls_<table>_<name>`: a lowercase
            identifier. Defaults to `None`, the column's name in the
            shorthand; required in the raw form.
        command: The command the policy covers: `"all"` (the default),
            `"select"`, `"insert"`, `"update"` or `"delete"`.
        restrictive: Make the policy `AS RESTRICTIVE`, so it must hold
            alongside every other policy instead of being one alternative.
            Defaults to `False`.
        column: Shorthand: the model column holding the scope value.
        setting: Shorthand: the dotted Postgres setting to compare it with,
            such as `"app.tenant_id"`.
        using: Raw: the `USING` expression, which decides the rows a command
            can see.
        with_check: Raw: the `WITH CHECK` expression, which decides the rows
            a write may leave behind.

    Raises:
        TypeError: The two forms are mixed or neither is given, a clause is
            one the command does not accept, or a name, command or flag has
            the wrong shape.
        ValueError: `setting` is not a dotted custom setting name.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import RowPolicy

        tenant = RowPolicy(column="tenant_id", setting="app.tenant_id")
        invitee = RowPolicy(
            name="invitee_read",
            command="select",
            using="id IN (SELECT document_id FROM membership)",
        )
        assert (tenant.resolved_name, invitee.resolved_name) == ("tenant_id", "invitee_read")
        ```
    """

    name: str | None = None
    """The policy name's suffix, or `None` for the shorthand's column name."""
    command: str = "all"
    """The command the policy covers."""
    restrictive: bool = False
    """Whether the policy is `AS RESTRICTIVE`."""
    column: str | None = None
    """Shorthand: the column holding the scope value."""
    setting: str | None = None
    """Shorthand: the Postgres setting the column is compared with."""
    using: str | None = None
    """Raw: the `USING` expression."""
    with_check: str | None = None
    """Raw: the `WITH CHECK` expression."""

    def __post_init__(self) -> None:
        self._validate_command()
        shorthand = self.column is not None or self.setting is not None
        raw = self.using is not None or self.with_check is not None
        if shorthand and raw:
            raise TypeError(
                "RowPolicy takes either the column/setting shorthand or the raw "
                "using=/with_check= form, not both. Drop column=/setting= to write "
                "the expression yourself, or drop using=/with_check= to let ferro "
                "render the current_setting() comparison."
            )
        if not shorthand and not raw:
            raise TypeError(
                "RowPolicy needs an expression: either the shorthand "
                "(column='tenant_id', setting='app.tenant_id') or the raw form "
                "(name='...', using='<sql>')."
            )
        if shorthand:
            self._validate_shorthand()
        else:
            self._validate_raw()
        self._validate_name()
        if not isinstance(self.restrictive, bool):
            raise TypeError(
                f"RowPolicy restrictive= must be a bool, not "
                f"{type(self.restrictive).__name__}"
            )

    def _validate_command(self) -> None:
        if not isinstance(self.command, str) or self.command not in COMMANDS:
            raise TypeError(
                f"RowPolicy command={self.command!r} is not a supported command: "
                f"expected one of {', '.join(repr(c) for c in COMMANDS)}."
            )

    def _validate_shorthand(self) -> None:
        if not isinstance(self.column, str) or not self.column:
            raise TypeError(
                "RowPolicy shorthand needs column= to name a column on this model, "
                f"not {self.column!r}."
            )
        if not isinstance(self.setting, str) or not self.setting:
            raise TypeError(
                f"RowPolicy(column={self.column!r}) needs setting= to name the "
                "Postgres setting holding the scope value, e.g. "
                "setting='app.tenant_id'."
            )
        if not _SETTING_KEY_RE.match(self.setting):
            raise ValueError(
                f"RowPolicy setting={self.setting!r} is not a custom Postgres "
                "setting key: expected dotted identifiers (e.g. 'app.tenant_id') "
                f"matching {_SETTING_KEY_SHAPE}. A key must be namespaced — "
                "built-in settings are not tenancy scope — and may contain only "
                "identifier characters, so it can never carry a quote into the "
                "rendered policy."
            )

    def _validate_raw(self) -> None:
        for label, value in (("using", self.using), ("with_check", self.with_check)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise TypeError(
                    f"RowPolicy {label}= must be a non-empty SQL expression string, "
                    f"not {value!r}."
                )
        if self.name is None:
            raise TypeError(
                "RowPolicy in the raw using=/with_check= form requires name=: ferro "
                "derives a shorthand policy's name from its column, but a raw "
                "expression has no column to name it after (e.g. "
                "RowPolicy(name='invitee_read', command='select', using='...'))."
            )
        if self.using is not None and not _COMMAND_CLAUSES[self.command]["using"]:
            raise TypeError(
                f"RowPolicy(name={self.name!r}, command={self.command!r}) declares "
                "using=, but Postgres accepts USING only on policies that read "
                "existing rows. A FOR INSERT policy validates new rows: use "
                "with_check= instead."
            )
        if (
            self.with_check is not None
            and not _COMMAND_CLAUSES[self.command]["with_check"]
        ):
            raise TypeError(
                f"RowPolicy(name={self.name!r}, command={self.command!r}) declares "
                "with_check=, but Postgres accepts WITH CHECK only on policies that "
                f"write rows. A FOR {self.command.upper()} policy filters existing "
                "rows: use using= instead."
            )
        if self.command == "insert" and self.with_check is None:
            raise TypeError(
                f"RowPolicy(name={self.name!r}, command='insert') needs with_check=: "
                "a FOR INSERT policy decides which new rows may be written."
            )
        if self.command != "insert" and self.using is None:
            raise TypeError(
                f"RowPolicy(name={self.name!r}, command={self.command!r}) needs "
                "using=: without it the policy would not filter any rows."
            )

    def _validate_name(self) -> None:
        name = self.resolved_name
        if not isinstance(name, str):
            raise TypeError(f"RowPolicy name must be a str, not {type(name).__name__}")
        if name.startswith("rls_"):
            raise TypeError(
                f"RowPolicy name {name!r} must not include the 'rls_<table>_' "
                "prefix: ferro derives the full policy name from the table and the "
                "name (e.g. RowPolicy(name='tenant', ...) becomes "
                "'rls_ledgerrow_tenant')."
            )
        if not _NAME_RE.match(name):
            raise TypeError(
                f"RowPolicy name {name!r} is not a valid identifier: expected "
                f"{_NAME_SHAPE} (lowercase letters, digits, and underscores, "
                "starting with a letter)."
            )

    @property
    def resolved_name(self) -> str:
        """The policy name's suffix: `name`, or the column in the shorthand."""
        if self.name is not None:
            return self.name
        return self.column  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"RowPolicy(name={self.resolved_name!r}, command={self.command!r})"


@dataclass(frozen=True)
class RowSecurity:
    """A model's row security: its policies and the table's flags.

    Assign it to the model's `__ferro_rls__`. On Postgres the table gets
    `ENABLE ROW LEVEL SECURITY` and one `CREATE POLICY` per policy; on
    SQLite, which has no row security, Ferro warns and creates the table
    without them. See
    [Scope rows by tenant](../guide/row-level-security.md) for the setup.

    Args:
        *policies: The table's policies.
        force: Also emit `FORCE ROW LEVEL SECURITY`, so the policies apply to
            the table's owner too. Defaults to `True`; without it, an
            application that connects as the owner is never filtered. See
            [`FORCE` and `BYPASSRLS`](../concepts/row-level-security.md#force-and-bypassrls).

    Raises:
        TypeError: A policy is not a [`RowPolicy`][ferro.RowPolicy], or
            `force` is not a `bool`.

    Examples:
        ```python {test=run prelude=reference}
        from typing import ClassVar
        from uuid import UUID

        from ferro import Field, Model, RowPolicy, RowSecurity


        class Invoice(Model):
            id: int | None = Field(default=None, primary_key=True)
            tenant_id: UUID
            total: int

            __ferro_rls__: ClassVar = RowSecurity(
                RowPolicy(column="tenant_id", setting="app.tenant_id")
            )
        ```

        On Postgres, `Invoice`'s table is created with:

        ```sql
        ALTER TABLE "invoice" ENABLE ROW LEVEL SECURITY
        ALTER TABLE "invoice" FORCE ROW LEVEL SECURITY
        CREATE POLICY "rls_invoice_tenant_id" ON "invoice" FOR ALL
          USING ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK ("tenant_id" = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        ```
    """

    policies: tuple[RowPolicy, ...]
    """The table's policies, in declaration order."""
    force: bool = True
    """Whether the table also gets `FORCE ROW LEVEL SECURITY`."""

    def __init__(self, *policies: RowPolicy, force: bool = True) -> None:
        object.__setattr__(self, "policies", tuple(policies))
        object.__setattr__(self, "force", force)
        if not isinstance(force, bool):
            raise TypeError(
                f"RowSecurity force= must be a bool, not {type(force).__name__}"
            )
        for index, policy in enumerate(self.policies):
            if not isinstance(policy, RowPolicy):
                raise TypeError(
                    f"RowSecurity()[{index}] must be a RowPolicy object (e.g. "
                    "RowPolicy(column='tenant_id', setting='app.tenant_id')), not "
                    f"{type(policy).__name__}"
                )

    def __repr__(self) -> str:
        return f"RowSecurity(policies={len(self.policies)}, force={self.force!r})"


def _declared_row_security(model_cls: type[Any]) -> RowSecurity | None:
    """Validate and return the declared ``__ferro_rls__``, or ``None``."""
    raw = getattr(model_cls, FERRO_RLS, None)
    if raw is None:
        return None
    if not isinstance(raw, RowSecurity):
        raise TypeError(
            f"{model_cls.__qualname__}.{FERRO_RLS} must be a RowSecurity object "
            "(e.g. RowSecurity(RowPolicy(column='tenant_id', "
            f"setting='app.tenant_id'))), not {type(raw).__name__}"
        )
    return raw


def compile_row_security(
    model_name: str,
    table_name: str,
    declaration: RowSecurity | None,
    columns: Mapping[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Validate ``__ferro_rls__`` and lower it into the SchemaIR wire shape.

    The single validate-and-lower choke point for row security: the SchemaIR
    compiler calls it on every compile pass with the column IR that pass just
    built, so an unknown or unsupported shorthand column fails at class
    definition rather than at connect.

    Args:
        model_name: Registry key / model class name, for error messages.
        table_name: Physical table name — the ``rls_<table>_<name>`` prefix.
        declaration: The model's ``RowSecurity``, or ``None``.
        columns: This compile's column IR objects, keyed by column name.

    Returns:
        The ``row_security`` payload object, or ``None`` when nothing is
        declared (absent, not empty, so an undeclared model's envelope and
        fingerprint are byte-identical to before).

    Raises:
        TypeError: For any declaration error — a duplicate policy name, an
            unknown shorthand column, or a column type the shorthand cannot
            cast.
    """
    if declaration is None:
        return None

    entries: list[dict[str, Any]] = []
    # Keyed on the LIVE name, not the declared suffix: `rls_<table>_<name>` is
    # truncated to Postgres's 63-byte limit, so two long, distinct suffixes can
    # land on one identifier. Deduping the suffix would let that pair through
    # and emit two CREATE POLICY statements with the same name — the second
    # fails mid-create, which is precisely the half-policed table the per-table
    # transaction exists to prevent. Catch it at class definition instead.
    seen: dict[str, str] = {}
    for policy in declaration.policies:
        suffix = policy.resolved_name
        live_name = _ddl_row_policy_name(table_name, suffix)
        collides_with = seen.get(live_name)
        if collides_with == suffix:
            raise TypeError(
                f"{model_name}.{FERRO_RLS} declares the duplicate row-policy name "
                f"{suffix!r}; each name identifies a distinct policy and must be "
                "unique per model."
            )
        if collides_with is not None:
            raise TypeError(
                f"{model_name}.{FERRO_RLS} policies {collides_with!r} and {suffix!r} "
                f"both resolve to the live policy name {live_name!r}: policy names "
                "are truncated to PostgreSQL's 63-byte identifier limit, and these "
                "two no longer differ once truncated. Shorten or rename one."
            )
        seen[live_name] = suffix
        entries.append(
            {
                "name": live_name,
                "command": policy.command,
                "restrictive": policy.restrictive,
                "expr": _policy_expr(model_name, policy, columns),
            }
        )
    return {"force": declaration.force, "policies": entries}


def _policy_expr(
    model_name: str, policy: RowPolicy, columns: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    """Lower one policy's expression source, checking the shorthand's column."""
    if policy.column is None:
        expr: dict[str, Any] = {"kind": "raw"}
        if policy.using is not None:
            expr["using"] = policy.using
        if policy.with_check is not None:
            expr["with_check"] = policy.with_check
        return expr

    column_ir = columns.get(policy.column)
    if column_ir is None:
        raise TypeError(
            f"{model_name}.{FERRO_RLS} policy {policy.resolved_name!r} references "
            f"unknown column {policy.column!r}. Valid columns: "
            f"{', '.join(sorted(columns))}."
        )
    _assert_shorthand_castable(model_name, policy, column_ir)
    return {"kind": "setting", "column": policy.column, "setting": policy.setting}


def _assert_shorthand_castable(
    model_name: str, policy: RowPolicy, column_ir: dict[str, Any]
) -> None:
    """Reject a shorthand column whose storage the ``NULLIF`` cast cannot take.

    The decision itself is the shared Rust one the emitters render with
    (``row_policy_shorthand_cast``), consumed here over FFI so the class
    definition fails for exactly the columns DDL would fail for (I-1).
    """
    decision = json.loads(_rls_shorthand_cast(json.dumps(column_ir)))
    if decision["supported"]:
        return
    raise TypeError(
        f"{model_name}.{FERRO_RLS} policy {policy.resolved_name!r} cannot use the "
        f"column/setting shorthand: {decision['reason']}. The shorthand casts the "
        "setting to the column's own type and supports uuid, text/varchar and the "
        "integer families. Write the comparison yourself with the raw form, e.g. "
        f'RowPolicy(name={policy.resolved_name!r}, using="{policy.column} = '
        f"current_setting('{policy.setting}', true)::<type>\")."
    )
