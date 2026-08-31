# Row-policy drift: ferro rebuilds the bodies it writes and reports the bodies you write; the flags are one-way

Row-security reconciliation on a live table follows the check family
(ADR-0013/0015) with two decisions of its own.

**Drift.** A live `rls_*` policy's `command`, permissive/restrictive flag,
which clauses it carries, and **the roles it applies to** (`pg_policy.polroles`)
are compared exactly — those are ferro's own metadata, not the server's
rendering — and any difference is a rebuild (`DROP POLICY` + `CREATE POLICY`,
metadata-only: no row is read or validated). Roles are in that list because
ferro's `CREATE POLICY` never writes a `TO` clause, so PUBLIC (`polroles = {0}`)
is the declared value: an `ALTER POLICY … TO admin_role` on a RESTRICTIVE tenant
fence stops it restricting the application role and the table fails **open**,
with the policy's expression completely unchanged. A rebuild converges back to
PUBLIC. Bodies are
compared as ADR-0015 compares check bodies: ferro's canonical render against
`pg_get_expr`, both through one normalizer
(`ferro_ddl_lowering::normalize_row_policy_expr`), pinned against real catalog
output. A **shorthand** body that differs is a rebuild: ferro rendered it, so
ferro knows what the catalog stores for it. A **raw** `using=`/`with_check=`
body that differs is reported with both texts and left alone. Postgres stores
its own rewriting of author SQL — `BETWEEN a AND b` comes back as
`(x >= a) AND (x <= b)` — and no text normalizer can undo a structural rewrite,
so rebuilding on a textual difference would re-drift on the next connect and
take an exclusive lock on the table at every boot, forever.

**The flags.** `migrate_updates` can only turn row security on. A live table
whose model dropped `__ferro_rls__`, or dropped `force=`, keeps its flags and
warns on every connect (`emit_user_warning_always`, so the warning registry can
never quiet it after the first boot). Orphaned `rls_*` policies warn the same
way. `migrate_destructive` is the only door that drops a policy or clears a
flag, and the run that does it says what it took away. Policies ferro does not
own (any name that is not `rls_*`) are reported and never altered, on any flag.

**Teardown scope.** Everything that removes row security, and every warning that
accuses someone of removing it, is gated on evidence that ferro managed the
table: at least one live `rls_*` policy. `relrowsecurity` carries no authorship,
so a table a DBA enabled RLS on by hand — with only their own policies — is left
completely alone, whatever flags a `migrate_destructive` run is exercising for an
unrelated column. Without that gate, one destructive migration would disable a
fence ferro never installed while the same run's foreign-policy report promised
it never touches those policies, and every ordinary connect would accuse the DBA
of dropping a declaration they never wrote — turning the one warning class that
must never be noise into exactly that.

Decision by owner (2026-08-31), PRD #406 / #413.

Rejected alternatives:

- **Rebuild every raw policy whose text differs**: converges only for
  expressions Postgres re-spells token-for-token. `BETWEEN`, and anything else
  the deparser restructures, would drop and recreate the policy on every
  connect — ADR-0015 rejected that shape for checks, and an exclusive lock at
  every boot is worse than the difference.
- **Silence about raw-body drift**: the same failure ADR-0015 rejected as
  "name-only identity" — the old body keeps enforcing and nobody is told.
- **Store ferro's canonical text in `COMMENT ON POLICY`**: a second ownership
  channel beside the `rls_` prefix, and one a `COMMENT` strip would turn into
  phantom drift. PRD #406 settled on one normalizer.
- **Deparse the declared expression through a temp view to compare exactly**:
  correct, and it makes the drift decision need a live connection and a DDL
  probe per policy per connect — a decision that must stay a pure function of
  (declaration, catalog) so the Alembic operation and the runtime pass can
  share it (I-1).
- **Turning flags off on `migrate_updates`**: a table that stops filtering rows
  because someone deleted a ClassVar is exactly the failure this feature
  exists to prevent.

## Consequences

- An unchanged declaration plans nothing, which is pinned against real
  `pg_get_expr` output for every shorthand cast and for the raw shapes the docs
  recommend (membership sub-select, function call, boolean composition, JSON
  claims).
- The normalizer's limits are stated on the function and tested: a structural
  rewrite reads as a difference (and is therefore reported, not rebuilt), while
  select-list aliases and column qualifiers inside sub-selects are dropped, so
  a change confined to one of those does not read as drift.
- A raw policy that ferro cannot compare is a warning per connect, not DDL.
  Expressing the policy in a form ferro renders (the column/setting shorthand)
  or dropping the policy so ferro recreates it are both ways out.
- When a raw policy rebuilds for its *metadata* and its body also fails to
  compare, the rebuild carries the declared body — the declaration is the truth
  — and the plan says which live text it replaced. A metadata fix never
  silently overwrites SQL ferro had just called unverifiable.
- The normalizer errs toward reporting a difference wherever it cannot be sure:
  parentheses around an operator it does not model are kept, a cast on anything
  but a string literal or a column reference stays significant, and an
  expression carrying SQL it cannot lex (`E'…'`, `$$…$$`, comments) is compared
  verbatim. False *equality* is the only unrecoverable direction, because it
  reads a drifted policy as unchanged; a false difference on a raw body is a
  warning.
