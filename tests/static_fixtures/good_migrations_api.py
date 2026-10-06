"""Type-checked (never executed): the in-process migration calls must PASS
`ty check` (#521).

test_static_contracts.py asserts this file produces no diagnostics: each
call's signature and return type, and the attributes of the three
exceptions, are what ``ferro.migrations`` publishes.
"""

from typing import assert_type

import ferro
from ferro.migrations import (
    DatabaseAheadError,
    MigrationRefused,
    PendingMigrationsError,
    check,
    require_applied,
    status,
    up,
)
from ferro.migrations.generate import CheckReport
from ferro.migrations.report import StatusReport
from ferro.migrations.runner import RunReport


async def adopt(settings: ferro.FerroSettings) -> None:
    assert_type(await up(), RunReport)
    assert_type(
        await up(
            settings,
            "main",
            using="reporting",
            lock_timeout="5s",
            allow_ahead=True,
        ),
        RunReport,
    )
    assert_type(await require_applied(), None)
    assert_type(await require_applied(settings, "main", allow_ahead=True), None)
    assert_type(await status(using="reporting"), StatusReport)
    report = await check(settings, "main")
    assert_type(report, CheckReport)
    report.raise_for_problems()

    try:
        await require_applied()
    except PendingMigrationsError as err:
        assert_type(err.pending, list[str])
        assert_type(err.refusals, list[str])
    except DatabaseAheadError as err:
        assert_type(err.ahead, list[str])
    except MigrationRefused as err:
        assert_type(err, MigrationRefused)
    assert issubclass(MigrationRefused, ferro.FerroError)
