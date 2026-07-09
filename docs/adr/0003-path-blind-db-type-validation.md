# Path-blind db_type validation at spec compilation

`db_type` validation historically ran only on the ferro declaration path
(`metaclass._validate_db_type_options` iterates `ferro_fields`), while the raw
path (`json_schema_extra={"db_type": ...}`) flowed into `ColumnSpec` unchecked —
`db_type="banana"` or `db_type="text"` on an `int` field passed class definition
silently. That gap was mostly harmless while unrecognized tokens fell through
the Rust storage resolver, but every token the resolver *does* recognize turns
it into a silent-miscoercion generator: a raw-path `db_type="jsonb"` on an
`int` field would resolve to JSONB storage on an integer column and fail at
DDL/bind time instead of import time — the exact class of per-path divergence
bug ADR-0002 exists to kill.

We move token + compatibility validation to the spec-compilation site
(`ferro.columns.build_column_specs`), where both declaration paths already
converge and the annotation is in hand. One vocabulary, one compatibility
matrix, both paths, all tokens; incoherent declarations raise `TypeError` at
class-definition time regardless of syntax. `db_check` validation stays at the
ferro seam (it is a ferro-path-only feature).

The declarable vocabulary is the Python canonical set — extended by `json` and
`jsonb` (see ADR-0004) — plus `varchar(N)`. Tokens the Rust resolver recognizes
but the vocabulary does not list (`blob`, `boolean`, `double`, `numeric`,
`bytea`, bare `varchar`, `char(N)`) remain undeclarable; adding one later means
adding it to the vocabulary *with* a compatibility predicate, never bypassing
validation.

## Considered options

- **Validate only the ferro path** — rejected. Satisfies loud-failure for one
  declaration syntax and violates it for the other; a per-path behavior split
  keyed on syntax is the disease ADR-0002 names.
- **Validate only the `jsonb` token on the raw path** — rejected. A one-token
  special case is a stopgap by construction and leaves the open door for every
  future token.

## Consequences

- Raw-path models carrying a token outside the declarable vocabulary now raise
  `TypeError` on import. Previously those declarations either did nothing
  (unrecognized tokens fell through the resolver) or worked undocumented
  (Rust-recognized tokens like `blob`); both were silent lies about what the
  declaration controlled. The loud failure is the point.
- The compatibility matrix in `ferro._annotation_utils` is now the single gate
  for both paths — a token accepted there must also be handled by
  `ferro_ddl_lowering::db_type_token_to_canonical`, and vice versa for
  declarable tokens (pinned by the cross-emitter parity suite).
