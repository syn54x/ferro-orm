# JSONB as a Postgres-only first-class canonical type

Adding the `jsonb` storage token (issue #260), we model JSONB as its own
`CanonicalType::Jsonb` variant rather than a "binary" flag alongside
`CanonicalType::Json`. JSONB genuinely is a distinct storage type on Postgres
(binary on-disk format, distinct DDL spelling, distinct parameter OID), and a
variant keeps every downstream decision — DDL render, migrate diff, Alembic
mapping, bind cast — an exhaustive match arm the compiler enumerates. A flag
would be plumbed as a side-channel through canonicalization, diff, and codec
independently, each free to disagree: the "one fact, two visibility rules"
shape ADR-0002 eliminated.

Three companion decisions bound the variant's reach:

1. **`Jsonb` never exists on SQLite.** The token lowers at the
   token→canonical seam — `db_type_token_to_canonical("jsonb")` returns `Json`
   on SQLite and `Jsonb` on Postgres — exactly how `boolean→Integer` and
   `uuid→Char(32)` already lower. No SQLite renderer, drift-class, or codec
   code ever sees a `Jsonb`.
2. **No `ColumnCodec::Jsonb` — the codec is genuinely unchanged.** Hydration
   and decode are identical for json and jsonb (the driver seam already
   decodes both wire formats to `serde_json::Value`), so the logical codec
   stays `ColumnCodec::Json`. The existing `::json` bind cast also stays:
   Postgres ships an assignment cast `json → jsonb` (`pg_cast` castcontext
   `a`), so INSERT/UPDATE parameters cast as `json` coerce server-side into a
   jsonb column — verified empirically against a prepared INSERT during
   implementation, falsifying this ADR's earlier draft claim that the bind
   cast had to become storage-aware. Operator contexts (`WHERE col = $1`) have
   no `jsonb = json` operator, but whole-column equality has no `json = json`
   operator either — both live with the deferred query-operators work, not
   here.
3. **Introspection stops collapsing.** `information_schema_to_db_type_token`
   maps live `jsonb` to `"jsonb"` (previously folded into `"json"`), so
   declared-vs-live diffs are honest: a jsonb declaration matches a live jsonb
   column, and a `json↔jsonb` edit is exactly one ALTER on Postgres. On SQLite
   both tokens lower to the same canonical, so the same edit is a no-op there.

Both `json` and `jsonb` join the declarable vocabulary with a single
json-family eligibility predicate (`dict`/`list` in any parameterization,
`BaseModel` subclasses); wider shapes (`TypedDict`, dataclass, `set`, `tuple`)
are rejected until asked for — loosening later is non-breaking, tightening is
not.

## Considered options

- **`Json` + binary flag on `SchemaColumn`** — rejected; see above.
- **Bare `postgresql.JSONB()` in the Alembic bridge** — rejected. It is a
  dialect-locked SA type that fails to compile on SQLite; the bridge's contract
  is one SA type per token with SQLAlchemy carrying the dialect split, so the
  mapping is `sa.JSON().with_variant(postgresql.JSONB(), "postgresql")`.
- **Widening eligibility to everything with json logical type** — rejected for
  now; the annotation predicate stays consistent with how every other token
  validates, and the narrow set errs in the reversible direction.

## Consequences

- Round-trip contract is Python `==` equality, not representation fidelity:
  Postgres jsonb does not preserve dict key order (keys come back sorted).
  Documented on the storage-types page; users needing verbatim order keep the
  default `json`. The round-trip suite pins this with an order-scrambled dict.
- The previously pinned introspection test asserting `jsonb → "json"` flips to
  `jsonb → "jsonb"` — an intended behavior change, not a regression.
- `canonical_to_db_type_token(Jsonb, Sqlite)` cannot occur (lowering happens
  before canonicalization on SQLite); the Postgres arm returns `"jsonb"`.
