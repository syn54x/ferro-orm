# Table-check reconciliation follows the destructive ladder, with a loud leftover warning

Every ferro-owned `ck_*` is in this pass — table checks and column checks (`Field(db_check=True)`). The column-check Field API does not change. A check missing from a live table is added on `migrate_updates`; same-name body drift is a constraint rebuild on that same pass. Both validate existing rows and fail the connect if they don't pass. An orphaned ferro-owned `ck_*` (live, gone from the model) drops only on `migrate_destructive`. Under `migrate_updates` it stays and ferro warns with the live name — leftover CHECKs keep rejecting rows the model now allows, so silence (the index-orphan behavior) is not acceptable. User-created CHECKs are never touched. Alembic autogenerate always reports add, body-rebuild, and drop — the auto-migrate gates are connect-time safety; a generated revision is not applied until someone reviews it. Parity is the decision and the SQL, not the flag (ADR-0011).

Decision by owner (2026-08-18), grilling #339.

Rejected alternatives:

- **Drop orphans on `migrate_updates`**: CHECK drop isn't data loss, but it breaks the destructive ladder indexes and uniques already use. A suffix rename would drop+add in one non-destructive pass; consistency with `idx_*`/`uq_*` won.
- **Never drop, like enum labels**: label removal can leave rows holding the value; a CHECK drop does not rewrite rows. Alembic-only drop would leave the model lying until someone writes a revision.
- **Silent leftover** (match orphaned indexes): an orphaned CHECK is user-visible — inserts the model accepts, the database rejects.
- **Table checks only**: would leave two populations of the same `ck_*` prefix, one reconciled and one not. Toggling `db_check=True` on an existing column would keep doing nothing.
