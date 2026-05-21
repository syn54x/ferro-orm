---
title: Typed WHERE filters with None panic or wrong SQL (IS NULL)
type: issue
tags: [gotcha, query, filter, is-null, bridge, ffi, rust, pyo3, sea-query, serde]
related_files:
  - src/query.rs
  - src/ferro/query/nodes.py
  - tests/test_typed_null_binds.py
  - docs/solutions/patterns/typed-null-binds.md
related_issues: [41, 61]
related_prs: [62]
captured: 2026-05-20
last_updated: 2026-05-20
---

## Problem

`Model.where(lambda t: t.col == None)` or `Model.col == None` / `!= None` used to
panic in Rust (`node_to_condition_for_backend`) or, if it did not panic, would
compile to `col = NULL`, which never matches rows in SQL.

## Takeaway

Python `None` on the filter RHS becomes JSON `"value": null`, which serde
deserializes as `Option<serde_json::Value>::None` — not `Some(Value::Null)`.
For `==` / `!=`, emit `IS NULL` / `IS NOT NULL` in `node_to_condition_for_backend`;
never `unwrap()` `node.value` before checking for a null RHS. Bind typing for
other operators stays in the typed-null bind pipeline — see
`docs/solutions/patterns/typed-null-binds.md`.

## Explanation

**Causal chain**

1. `QueryNode.to_dict()` serializes Python `None` as JSON `null` on `value`.
2. Rust `QueryNode { value: Option<serde_json::Value> }` deserializes absent/null
   as `None`, not `Some(Null)`.
3. Pre-fix code: `let val = node.value.as_ref().unwrap();` → panic across FFI
   (AGENTS.md I-3).
4. Even without panic, `col.eq(bind_null)` yields `col = NULL`, which is always
   unknown/false in three-valued logic — filters return no rows.

**Fix (PR #62, issue #41)**

```rust
let rhs_is_json_null = node.value.as_ref().map_or(true, serde_json::Value::is_null);

let expr: SimpleExpr = if rhs_is_json_null {
    match node.operator.as_str() {
        "==" => col.is_null(),
        "!=" => col.is_not_null(),
        // other ops: value_rhs_simple_expr_for_backend(..., &Value::Null, ...)
        ...
    }
} else {
    let val = node.value.as_ref().unwrap();
    // existing non-null paths
};
```

**Discovery before fix (session history)**

During the April `refactor/typed-null-binds` work, the same wire shape was
identified and GitHub #41 was filed; `test_filter_by_none_does_not_reproduce_38`
stayed `xfail(strict=True)` until this fix landed on branch
`cursor/fix-typed-predicate-null-is-null-f3a4`.

**Tests**

| Layer | File | What it pins |
|-------|------|----------------|
| Integration | `tests/test_typed_null_binds.py` | `test_filter_by_none_does_not_reproduce_38` — FieldProxy `== None` / `!= None` |
| Integration | `tests/test_typed_null_binds.py` | `test_lambda_predicate_null_filter_datetime_and_json` — QueryProxy lambda on nullable datetime + JSON |
| Rust unit | `src/query.rs` | `json_null_deserializes_to_option_none_for_query_node_value` |
| Rust unit | `src/query.rs` | `where_rhs_none_emits_is_null_for_eq_sqlite` / `where_rhs_none_emits_is_not_null_for_ne_sqlite` |

## How to recognize

- Crash or opaque unwind when filtering with `== None` / `!= None` on any column
  type (including `datetime | None` and `list | None` / JSON).
- Grep finds `node.value.as_ref().unwrap()` in `node_to_condition_for_backend`
  without a prior null-RHS guard.
- Generated SQL contains `= null` instead of `is null` for None filters.
- Distinct from issue #38 (typed **bind** `null::text` on INSERT) and #56 (SQLite
  **fetch** NULL → `int(0)`); this is the **WHERE compile** path in `src/query.rs`.

## Prevention

- Treat JSON `null` on optional Rust fields as “SQL null intent,” not as “missing
  field” — use `map_or(true, Value::is_null)` (or equivalent) before `unwrap`.
- For every filter API that accepts Python `None`, add both FieldProxy and
  QueryProxy lambda integration tests plus a Rust unit test on deserialized
  `{"value": null}`.
- Keep the invariant in `typed-null-binds.md` § “IS NULL for typed `== None`”
  when adding new schema-driven emitters.

## Related

- Pattern: `docs/solutions/patterns/typed-null-binds.md` (bind layer + IS NULL rule)
- Issue [#41]: https://github.com/syn54x/ferro-orm/issues/41
- Issue [#61]: https://github.com/syn54x/ferro-orm/issues/61 (duplicate report)
- PR [#62]: https://github.com/syn54x/ferro-orm/pull/62
- Issue [#38]: typed-null binds on Postgres (orthogonal root cause)

[#38]: https://github.com/syn54x/ferro-orm/issues/38
[#41]: https://github.com/syn54x/ferro-orm/issues/41
[#61]: https://github.com/syn54x/ferro-orm/issues/61
