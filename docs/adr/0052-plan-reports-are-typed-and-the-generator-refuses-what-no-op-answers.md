# Plan reports are typed, and the generator refuses what no op answers

Extends ADR-0032, ADR-0041 and ADR-0050.

A model carries a rename hint that two tables claim:

```python
class Author(Model):
    __ferro_renamed_from__ = "writer"

class Poet(Model):
    __ferro_renamed_from__ = "writer"
```

Every door has to refuse it, but the planner reports it only as a sentence: `rename hint refused: tables "author" and "poet" all declare …`. Each reader then works out the meaning again:

- The bridge matches the prefix `"rename hint refused"`.
- The pass calls `refuse_hints` a second time before it probes labels.
- The generator checks the hints itself before planning.

The generator also has to tell "a report this change caused" from "a report the models always raise". It does that by planning the target against itself, and the parent against itself, for every dialect, and subtracting the two lists of strings. To tell which leftover-check report a planned drop answers, it renders that report's sentence again and compares. The renderer's warnings are strings too: the generator and the bridge refuse an op whose rendering has a first warning, unless the op is a new table.

## The decision

Every report the planner, the renderer and the auto-migrate pass write is a typed value, and its text is byte-identical to today's sentence:

```rust
pub struct Report { pub kind: ReportKind, pub subject: Subject, pub text: String, pub recurs: bool }
impl Report {
    pub fn answered_by(&self, ops: &[MigrationOp]) -> bool;  // a planned op settles what the report says
    pub fn blocks(&self) -> bool;                            // a rendering that leaves its op out
}
```

`recurs` replaces the second warning channel (`always_warnings`): a recurring report is raised on every run until someone acts. A plan carries `reports: Vec<Report>`, and a rendered op carries its own.

| Source | Kinds |
|---|---|
| The planner (10) | `HintRefused(HintError)`, `LeftoverChecks { names }`, `ExtraEnumLabels { labels }`, `ForeignFkDrift { column, name }`, `DroppedRowSecurity`, `ForeignPolicies { names }`, `UnverifiablePolicy { name }`, `PolicyBodyReplaced { name }`, `RowSecurityTeardown { names }`, `ExtraPolicies { names }` |
| The renderer (3) | `RefusedConversion`, `SqliteInPlace { what }`, `RowSecuritySkipped` |
| The pass (5) | `PendingTableRename`, `StrandedLabelRename`, `RowSecurityUnderMigrator`, and ADR-0049's `RunLockWait` and `DdlLockRetry` |

- **A refused hint is a report, never an error.** The pass still plans the rest and warns. The generator and the bridge refuse on the kind. The pass's label probe reads the same report and no longer re-checks the hints.
- **The generator refuses every report no op of the same plan answers.** Between two declared snapshots, planned with every drop on, each report is one of two things:
  - one the plan answers, such as `LeftoverChecks` answered by the plan's `DropCheck`s, matched by subject;
  - a change the generator cannot write, such as `ForeignFkDrift` on a hand-named foreign key, or `HintRefused`.

  So no report needs to be classified as standing or caused, and the self-planning and string subtraction go.
- **A rendering blocks by its kind.**
  - `RefusedConversion` and `SqliteInPlace` block: the op is left out, so the generator refuses it and the bridge refuses or makes it irreversible.
  - `RowSecuritySkipped` does not block: the table is still created, and SQLite has no row security to install.
- **The auto-migrate pass's report (ADR-0049) carries the same type.** Its parity pins compare kinds and subjects, not sentences.

## Considered options

- **Keep the sentences and match prefixes.** Rejected: the matching is a second decision about the planner's meaning, made in each reader, and it breaks when a sentence is reworded.
- **Have the planner tag each report as standing or caused by the change.** Rejected once the kinds were listed: on a plan between two declared snapshots, every kind is either answered by an op or a refusal, so the tag would carry nothing.
- **Make a refused hint an `Err`.** Rejected: the pass would stop planning everything else because of one bad hint, where today it warns and goes on.

## Consequences

- `MigrationPlan`'s `warnings` and `always_warnings` become one `reports` list on the wire, and the Python readers (`drift`, `baseline`, the bridge, the translator) read kinds.
- The pass-recording fixtures stay byte-identical, because the text does not change.
- The generator's standing re-plans, its warning subtraction, the bridge's prefix match and the pass's second `refuse_hints` call are deleted.

## Amendments

- 2026-10-07: the renderer has a fourth kind, `PrimaryKeyKept` (a declared primary key differs from the live one, and no door changes a key in place), which blocks. A report's wire JSON carries `blocks`, computed by the one `Report::blocks`, and the Alembic bridge reads only that.
- 2026-10-08: the renderer has a fifth kind, `EnumTypeMove`, which blocks. `mood: Mood` (a native Postgres enum type) becoming `mood: str`, or back, is no statement on any door: the verdict refuses it (`Refusal::EnumTypeMove`), and the renderer, which the reconciliation pass reads, reports it instead of keeping the column silently (from an enum) or warning with an Alembic recipe (to one, the retired `RefusedConversion::VarcharToPgEnum`). Both texts are one constructor's, `enum_type_move_report`, so the pass prints the generator's recipe word for word. The sentence holds on both doors (no statement converts the column in place; `ferro migrate new` does it), and a move *to* an enum type also names the fix that keeps the values in a text column: declare the field with `db_type="text"` (a `varchar(N)` column keeps its own token; `string_storage_token`). SQLite stores an enum as text: there is no type to move.
