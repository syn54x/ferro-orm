"""The chunk loop: a chunked data step run in batches that resumes (ADR-0024).

```python
@chunked(
    lambda models: models.Author.where(lambda author: author.slug == None)
    .order_by(lambda author: author.id),
    batch_size=1000,
)
async def up(ctx, batch):
    for author in batch:
        author.slug = slugify(author.name)
        await author.save()
```

Over 2,500 authors the runner opens three transactions. Each reads the next
1,000 rows after the cursor (keyset ``after()`` over the query's order
keys), hands them to ``up(ctx, batch)``, and commits, in that same
transaction, the step record's cursor and ``rows_done``::

    batch 1  rows 1-1000      resume_cursor {"keys": [1000], "rows_done": 1000}
    batch 2  rows 1001-2000   resume_cursor {"keys": [2000], "rows_done": 2000}
    batch 3  rows 2001-2500   the finished record, rows_done 2500

A batch that fails rolls back alone: the batches before it stay committed
with their cursor, and the next run starts after it, so no row is replayed.
The last batch (fewer rows than ``batch_size``, possibly none) writes the
finished record going up, or removes the record going down. A chunked down
pages its own query the same way, marking the record ``reverting`` with its
own cursor (``revert_cursor``) until that last batch (ADR-0033).

A failed down is recorded only where the database moved (the tracking
table says where it stands now). A down that rolled back completely (an
atomic down, a transactional SQL down, a chunked down failing on its first
batch) leaves the record unchanged: the step stays ``installed`` and the
error is in the run's output. A down that left part of itself applied
writes the error onto the record: a no-transaction SQL down, and a chunked
down failing after a committed batch, which stays ``reverting`` at its
cursor so ``up`` refuses until a ``down`` finishes it.

The cursor is the last row's order-key tuple as JSON, each value in the form
``canonicalize_wire_scalar`` gives a query literal (I-13), so ``after()``
compares a resumed cursor exactly as ``save()`` wrote the row.
"""

from __future__ import annotations

import enum
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import TypeAdapter

from .. import _core
from .._bind_payload import canonicalize_wire_scalar
from ..models import transaction
from ..state import resolve_operation_scope
from .steps import Chunked, StepRefused

if TYPE_CHECKING:
    from ..query import Query
    from ..raw import Transaction
    from .context import StepContext
    from .steps import Declared

__all__ = [
    "BatchFailed",
    "ChunkOutcome",
    "decode_cursor",
    "encode_cursor",
    "run_chunked",
]


@dataclass(frozen=True)
class ChunkOutcome:
    """What a finished chunk loop did."""

    rows_done: int
    """The rows the walk has committed, across every run of it."""
    batches: int
    """The batch transactions this run committed (one for an empty pass)."""


class BatchFailed(Exception):
    """A batch raised and rolled back; everything before it stays committed.

    Carries what the record holds now, so the runner can write the failure
    beside the committed cursor without moving it.
    """

    def __init__(
        self,
        error: Exception,
        *,
        cursor: str | None,
        rows_done: int,
        batches: int,
        committed: bool,
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.cursor = cursor
        """The last committed cursor (``None`` before any row)."""
        self.rows_done = rows_done
        self.batches = batches
        """The batches this run committed before the failing one."""
        self.committed = committed
        """Whether the walk has committed anything on the record: going up
        always (the started record); going down once the record is
        ``reverting``, from this run or an earlier one."""


# -- the cursor codec --------------------------------------------------------------


def _cursor_value(value: Any) -> Any:
    canonical = canonicalize_wire_scalar(value)
    if isinstance(canonical, enum.Enum):
        return canonical.value
    if isinstance(canonical, bytes | bytearray):
        raise TypeError(
            "a bytes order key cannot be a cursor value; order the chunked query by "
            "other columns and its primary key"
        )
    return canonical


def encode_cursor(keys: Sequence[Any], rows_done: int) -> str:
    """The cursor JSON for the last row's order-key values ``keys``:
    ``{"keys": [...], "rows_done": N}``, each value in its query-literal
    form (``canonicalize_wire_scalar``, I-13)."""
    return json.dumps(
        {"keys": [_cursor_value(key) for key in keys], "rows_done": rows_done}
    )


def decode_cursor(text: str, types: Sequence[Any]) -> tuple[tuple[Any, ...], int]:
    """The order-key values and ``rows_done`` of cursor JSON ``text``, each
    value validated back to its order key's annotation in ``types``."""
    cursor = json.loads(text)
    keys = cursor["keys"]
    if len(keys) != len(types):
        raise StepRefused(
            f"ferro migrate: the step's cursor holds {len(keys)} order-key values, "
            f"but its query now orders by {len(types)} keys; the step's query changed "
            f"since its last committed batch"
        )
    values = tuple(
        TypeAdapter(annotation).validate_python(value)
        for annotation, value in zip(types, keys, strict=True)
    )
    return values, int(cursor["rows_done"])


def _order_key_types(query: Query[Any]) -> tuple[Any, ...]:
    fields = query.model_cls.model_fields
    return tuple(fields[entry.column].annotation for entry in query.order_by_clause)


# -- the loop ----------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def run_chunked(
    ctx_factory: Callable[[Transaction], StepContext],
    declared: Declared,
    record: dict[str, Any],
    *,
    direction: Literal["up", "down"],
    using: str | None = None,
    tracking_schema: str | None = None,
    verify_lock: Callable[[], Awaitable[Any]] | None = None,
) -> ChunkOutcome:
    """Run ``declared`` (a ``@chunked`` step function) to its last batch.

    ``ctx_factory(tx)`` is the step's context on one batch's transaction;
    ``record`` is the step's record as it stands (going up, the started
    record the runner wrote before the step runs, carrying the cursor of an
    earlier attempt; going down, the standing record). Batches run on
    connection ``using``; ``verify_lock`` is awaited before each one
    (ADR-0029), and what it raises propagates untouched.

    Going up the cursor is ``resume_cursor`` and the last batch writes the
    finished record; going down it is ``revert_cursor`` with the record
    marked ``reverting``, and the last batch removes the record. Each batch
    is ``transaction(immediate=True)``: on SQLite a ``BEGIN IMMEDIATE`` that
    holds the write lock before the batch reads, so its read-then-write
    cannot fail upgrading into ``SQLITE_BUSY`` (ADR-0024); on Postgres a
    plain ``BEGIN``.

    Raises:
        BatchFailed: a batch raised; it rolled back, and what the record
            holds is on the exception.
    """
    shape = declared.shape
    if not isinstance(shape, Chunked):
        raise TypeError(f"run_chunked runs a @chunked step, not @{shape.kind}")
    down = direction == "down"
    migration, step = record["migration"], record["step"]
    stored = record["revert_cursor"] if down else record["resume_cursor"]
    if down and not record["reverting"]:
        stored = None
    committed = not down or bool(record["reverting"])
    cursor: str | None = stored
    rows_done = 0
    keys: tuple[Any, ...] | None = None
    batches = 0
    clock = time.monotonic()

    while True:
        if verify_lock is not None:
            await verify_lock()
        try:
            async with transaction(using=using, immediate=True) as tx:
                route = resolve_operation_scope(using=None, session=None)
                ctx = ctx_factory(tx)
                query = shape.query(ctx.models)
                if cursor is not None and keys is None:
                    keys, rows_done = decode_cursor(cursor, _order_key_types(query))
                page = query if keys is None else query.after(keys)
                batch = await page.limit(shape.batch_size).all()
                if batch:
                    keys = query.position_of(batch[-1])
                    await declared.fn(ctx, batch)
                    rows_done += len(batch)
                    cursor = encode_cursor(keys, rows_done)
                last = len(batch) < shape.batch_size
                if last and down:
                    await _core._remove_record(route, migration, step, tracking_schema)
                elif last:
                    finished = {
                        **record,
                        "finished_at": _now(),
                        "failed_at": None,
                        "error": None,
                        "resume_cursor": cursor,
                        "rows_done": rows_done,
                        "duration_ms": int((time.monotonic() - clock) * 1000),
                    }
                    await _core._write_record(
                        using, json.dumps(finished), tracking_schema, route
                    )
                else:
                    await _core._write_cursor(
                        using,
                        migration,
                        step,
                        cursor,
                        rows_done,
                        down,
                        tracking_schema,
                        route,
                    )
        except Exception as err:
            raise BatchFailed(
                err,
                cursor=stored,
                rows_done=_rows_of(stored),
                batches=batches,
                committed=committed,
            ) from err
        batches += 1
        stored, committed = cursor, True
        if last:
            return ChunkOutcome(rows_done=rows_done, batches=batches)


def _rows_of(cursor: str | None) -> int:
    return 0 if cursor is None else int(json.loads(cursor)["rows_done"])
