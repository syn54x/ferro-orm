# Omitted `nulls=` means last, not the dialect default

`order_by(..., nulls=)` accepts `"last"` | `"first"` | `"native"`. Omitted
means **`last`** — the same NULL placement on every backend. `"native"` is the
escape hatch for a backend's own default (Postgres and SQLite are opposites).
`native` is never implied.

#363 shipped omitted as dialect-native and deliberately did not cross-assert
omitted DESC. That split is the opposite of portable paging: the same
`after((3pm, 2))` would start in a different bucket per backend, and a
Postgres `DESC` with no `nulls=` puts NULLs *first* (unpinned conversations
leading). Defaulting to `last` is the typical "empties at the bottom" list
and matches the Pinch pinned-first shape.

This is a breaking change for every `order_by` that omitted `nulls=`, not
only for `after`/`before`:

- Postgres `DESC` — NULLs move first → last
- SQLite `ASC` — NULLs move first → last

Rejected: requiring `nulls=` only on nullable keys when paging (the kwarg
is required sometimes and not others); requiring it on every key only when
`after`/`before` is set (same smell); leaving omitted as `native` (pages
are not portable).

See `CONTEXT.md`: Null placement. Grilled with #372.
