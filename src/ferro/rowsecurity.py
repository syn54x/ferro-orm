"""Row security declared on Ferro models (PRD #406).

A model says once who is allowed to see its rows::

    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: UUID
        amount: float

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting="pinch.ledger_id")
        )

and ferro creates the table with row-level security switched on and the
matching policy::

    ALTER TABLE "ledgerrow" ENABLE ROW LEVEL SECURITY
    ALTER TABLE "ledgerrow" FORCE ROW LEVEL SECURITY
    CREATE POLICY "rls_ledgerrow_ledger_id" ON "ledgerrow" FOR ALL
      USING ("ledger_id" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid)
      WITH CHECK ("ledger_id" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid)

From then on the database itself decides which rows a query can see: a
connection whose ``pinch.ledger_id`` setting is unset sees **no** rows, and one
that carries a ledger id sees only that ledger's rows — a forgotten ``where``
filter is no longer a data leak.

This module owns the declaration surface and the validation that runs at class
definition. Every SQL decision — the policy name, the ``NULLIF`` expression,
the ``CREATE POLICY`` statement — lives in ``crates/ferro-ddl-lowering`` so
each emitter renders the same bytes (AGENTS.md § I-1).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._core import _ddl_row_policy_name, _rls_command_matrix, _rls_shorthand_cast

FERRO_RLS = "__ferro_rls__"

#: Which clauses Postgres accepts per command, read once from the shared
#: lowering layer. There is no second copy of this table here on purpose: the
#: renderer filters clauses with the same decision, so a command added in
#: ``ferro-ddl-lowering`` cannot drift out of the validation below (I-1).
_COMMAND_CLAUSES: dict[str, dict[str, bool]] = {
    row["command"]: row for row in json.loads(_rls_command_matrix())
}

#: The commands a policy may be scoped to (`FOR <command>`), in Rust's order.
COMMANDS = tuple(_COMMAND_CLAUSES)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\Z")
_NAME_SHAPE = "[a-z][a-z0-9_]*"


@dataclass(frozen=True)
class RowPolicy:
    """One row policy: which rows a command may touch.

    Two forms. The **shorthand** compares a column to a session setting::

        RowPolicy(column="ledger_id", setting="pinch.ledger_id")

    which renders ``"ledger_id" = NULLIF(current_setting('pinch.ledger_id',
    true), '')::uuid`` for both ``USING`` and ``WITH CHECK``. The cast comes
    from the column's own storage type; ``uuid``, ``text``/``varchar`` and the
    integer families are supported, and anything else is a class-definition
    error pointing here, at the raw form. The ``NULLIF`` is what makes an unset
    (or reset) setting mean *no rows* instead of a cast error.

    The **raw** form takes SQL and requires a name::

        RowPolicy(
            name="invitee_read",
            command="select",
            using="id IN (SELECT ledger_id FROM membership WHERE ...)",
        )

    Args:
        name: Policy name suffix; the live policy is ``rls_<table>_<name>``.
            Defaults to the column name in the shorthand form; required in the
            raw form.
        command: ``"all"`` (default), ``"select"``, ``"insert"``, ``"update"``
            or ``"delete"``.
        restrictive: ``True`` makes the policy AND-compose with every other
            policy (``AS RESTRICTIVE``) instead of OR-composing.
        column: Shorthand — the model column carrying the scope value.
        setting: Shorthand — the Postgres setting (GUC) key to compare it to.
        using: Raw — the ``USING`` expression, deciding which rows are visible.
        with_check: Raw — the ``WITH CHECK`` expression, deciding which rows
            may be written.

    Examples:
        >>> RowPolicy(column="ledger_id", setting="pinch.ledger_id")
        RowPolicy(name='ledger_id', command='all')
        >>> RowPolicy(name="owner_all", command="update", using="true")
        RowPolicy(name='owner_all', command='update')
    """

    name: str | None = None
    command: str = "all"
    restrictive: bool = False
    column: str | None = None
    setting: str | None = None
    using: str | None = None
    with_check: str | None = None

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
                "(column='ledger_id', setting='pinch.ledger_id') or the raw form "
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
                "setting='pinch.ledger_id'."
            )
        if "." not in self.setting:
            raise ValueError(
                f"RowPolicy setting={self.setting!r} is not a custom Postgres "
                "setting: the key must be namespaced with a dot (e.g. "
                "'pinch.ledger_id'). Built-in settings are not tenancy scope."
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
        """The policy's name suffix — ``name``, or the column in the shorthand."""
        if self.name is not None:
            return self.name
        return self.column  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"RowPolicy(name={self.resolved_name!r}, command={self.command!r})"


@dataclass(frozen=True)
class RowSecurity:
    """A model's row security declaration: its policies plus the table flags.

    ``force=True`` (the default) emits ``FORCE ROW LEVEL SECURITY`` as well as
    ``ENABLE``, so the table's owner is filtered too. Without it a deployment
    that connects as the table owner — the common single-role setup — gets
    policies that are never consulted.

    Examples:
        >>> RowSecurity(RowPolicy(column="ledger_id", setting="pinch.ledger_id"))
        RowSecurity(policies=1, force=True)
    """

    policies: tuple[RowPolicy, ...]
    force: bool = True

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
                    "RowPolicy(column='ledger_id', setting='pinch.ledger_id')), not "
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
            "(e.g. RowSecurity(RowPolicy(column='ledger_id', "
            f"setting='pinch.ledger_id'))), not {type(raw).__name__}"
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
    seen: set[str] = set()
    for policy in declaration.policies:
        suffix = policy.resolved_name
        if suffix in seen:
            raise TypeError(
                f"{model_name}.{FERRO_RLS} declares the duplicate row-policy name "
                f"{suffix!r}; each name identifies a distinct policy and must be "
                "unique per model."
            )
        seen.add(suffix)
        entries.append(
            {
                "name": _ddl_row_policy_name(table_name, suffix),
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
