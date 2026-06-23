"""Shared deprecation helpers for Ferro compatibility-window features."""

from __future__ import annotations

import functools
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

# Canonical IR-first compatibility window.
IR_FIRST_DEPRECATION_SINCE = "v0.12.0"
IR_FIRST_DEPRECATION_REMOVE_IN = "v0.13.0"

# Back-compat alias used by inventory/docs.
REMOVAL_RELEASE = IR_FIRST_DEPRECATION_REMOVE_IN

# Published migration guide (see zensical.toml site_url + nav).
IR_FIRST_MIGRATION_GUIDE = "https://syn54x.github.io/ferro-orm/howto/migrating-to-v0-12-0/"
IR_FIRST_MIGRATION_GUIDE_PREDICATES = (
    f"{IR_FIRST_MIGRATION_GUIDE}#1-use-lambda-predicates-in-where"
)
IR_FIRST_MIGRATION_GUIDE_SESSIONS = (
    f"{IR_FIRST_MIGRATION_GUIDE}#2-run-operations-inside-a-session"
)
IR_FIRST_MIGRATION_GUIDE_ALEMBIC = (
    f"{IR_FIRST_MIGRATION_GUIDE}#3-build-alembic-metadata-from-get_metadata"
)


def deprecation_message(
    *,
    reason: str,
    since: str,
    remove_in: str | None = None,
    reference: str | None = None,
) -> str:
    """Build a canonical Ferro deprecation warning message."""
    message = f"{reason.rstrip()} Deprecated since {since}."
    if remove_in is not None:
        message = f"{message} Planned removal in {remove_in}."
    if reference is not None:
        message = f"{message} See {reference.rstrip()}."
    return message


def warn_deprecated(
    *,
    reason: str,
    since: str,
    remove_in: str | None = None,
    reference: str | None = None,
    stacklevel: int = 2,
) -> None:
    """Emit a :class:`DeprecationWarning` for non-decorator call sites."""
    warnings.warn(
        deprecation_message(
            reason=reason,
            since=since,
            remove_in=remove_in,
            reference=reference,
        ),
        DeprecationWarning,
        stacklevel=stacklevel + 1,
    )


def deprecated(
    *,
    reason: str,
    since: str,
    remove_in: str | None = None,
    reference: str | None = None,
) -> Callable[[F], F]:
    """Mark a callable as deprecated and warn on each invocation."""
    message = deprecation_message(
        reason=reason,
        since=since,
        remove_in=remove_in,
        reference=reference,
    )

    def decorate(obj: F) -> F:
        @functools.wraps(obj)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            warnings.warn(message, DeprecationWarning, stacklevel=2)
            return obj(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def enable_deprecation_warnings() -> None:
    """Show :class:`DeprecationWarning` emitted from library code by default."""
    warnings.simplefilter("default", DeprecationWarning)


enable_deprecation_warnings()
