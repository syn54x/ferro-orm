# The test harness carries the targets and the promptless revert the application API refuses

Migration `0007` splits `User.full_name` into `first_name` and `last_name` with a backfill. The project wants a test that seeds a row the way the table stood *before* `0007`, runs the migration, and reads the row back:

```python
from ferro.migrations import testing as mt

@pytest.fixture
async def harness(fresh_db):
    return mt.harness(database="main", using=fresh_db)

async def test_0007_splits_full_name(harness):
    await harness.apply_through("0006")
    async with harness.models_at("0006") as m:
        await m.User(id=1, full_name="Ada Lovelace").save()
    await harness.apply("0007")
    async with harness.models_at("0007") as m:
        user = await m.User.get(1)
        assert (user.first_name, user.last_name) == ("Ada", "Lovelace")
```

Nothing in the application API can write that test. `up()` takes no target (ADR-0040), `down` is a CLI verb with a prompt (ADR-0038), and historical models exist only inside a data step (ADR-0035). Each of those is right where it was decided and wrong here, so the test helpers are a module of their own, `ferro.migrations.testing`, carrying exactly the facilities the other door refuses:

- `apply_through("0007")` applies every pending migration up to and including it; `apply("0007")` applies that one and refuses unless the database stands at its parent, so a test that seeded at `0006` knows only `0007` ran.
- `revert_to("0006")` and `revert_all()` run downs with no prompt. The runner's rules hold: an irreversible step in the way refuses before anything is reverted.
- `models_at("0006")` is a scope yielding the same `models` object a step context carries, built by the same builder from that one snapshot (not the union a step sees). While it is open today's classes are unreachable, exactly as inside a step.
- `round_trip()` applies every migration in order, reverts them all, and applies them again, asserting no drift against the snapshot the database should stand at after every stop. An irreversible step ends the downward walk at its migration, reported and not failed: everything above it round-trips, everything below is applied-only.

The harness is one object constructed per fixture, binding `settings=`, `database=` and `using=` once; it keeps no state beyond that binding and reads the tracking table on every call. It never makes a database fresh: SQLite is a new temporary file per test, Postgres a database per session with a schema or `TEMPLATE` copy per test, both the project's fixture. It never skips a dialect: the project's fixture parametrizes over the configured `dialects` and decides what to do when a URL is unset locally. No step-level target exists (`apply("0007:02")`): the state between two steps of one migration has no snapshot, so `models_at` would mean nothing there, and a backfill is tested by seeding at the parent and applying the whole migration.

**`drift()` and `check()` join the public calls** of ADR-0038, each returning the report its CLI verb prints and raising nothing; `raise_for_problems()` on the report raises carrying it, for a fixture that wants to abort the session in one line. With them the test that the chain reaches the models is two lines, and nothing new has to decide what "reaches" means:

```python
async def test_migrations_reach_the_models(harness):
    await harness.apply_through(head)
    assert (await ferro.migrations.drift()).is_clean   # live == head snapshot
    (await ferro.migrations.check()).raise_for_problems()  # models == head snapshot
```

The CLI gains `down --all`, so the harness never has a verb the operator lacks.

## Why the application API keeps refusing

ADR-0040 rejected `up(to=...)` because an application would carry a migration address in code that has to change with every release, and because the in-between state it names is one nothing declares. A test *is* about one migration by name: its address rots on purpose, and it is deleted with the migration it tests. ADR-0038 kept `down` on the CLI because reverting is an operator's decision with a prompt; a test database has no operator and nothing to lose. The reasons stand on their door and do not reach this one, which is why the facilities live in a module whose name says what it is for, not as flags on `up()`.

## Auto-migrate in tests

`connect(auto_migrate=True)` stays the recommended posture for *application* tests: one create pass from the models, independent of the chain's length, running no data step over an empty table. Its boundary is stated: it builds the schema the models declare today and proves nothing about the chain, which the migration tests above exercise once per suite. ADR-0038's guard never fires, since a test database carries no tracking table, and the two never meet in one database.

## Considered options

- **Nothing beyond the API, with a documented recipe.** Rejected: the recipe cannot be written; the API has no target and no revert.
- **A pytest plugin** (`pytest11` entry point, a `ferro-orm[test]` extra, auto-registered fixtures). Rejected for now: it would have to invent options for the URL and the database that a project's `conftest.py` already has, and would register fixtures into every project that installs ferro. It can be layered over the module later.
- **Targets on `up()` behind a flag.** Rejected: ADR-0040's reason is about the application door, and a flag on it reopens that door.
- **`assert_chain_reaches_models()`.** Rejected: a second spelling of "drift is empty" under a new name, hiding which of the two facts (live vs head snapshot, models vs head snapshot) failed.
- **Seeding and inspection by raw SQL only.** Rejected: the test re-spells the snapshot's columns by hand, which is what the snapshot builder exists to avoid.
- **A `reset()` on the harness.** Rejected: dropping only what the snapshot declares leaves user-owned objects and a failed test's stray table standing; dropping everything is a destructive verb on whatever URL the fixture was handed.
- **Step-level targets.** Rejected: no snapshot describes the state between steps.
- **`check()` raising on a broken chain.** Rejected: a test wants the whole list of problems in one failure, and the CLI then renders the same report and maps it to an exit code.
- **A `strict=True` parameter instead of `raise_for_problems()`.** Either works; the method keeps the call's return type fixed and the exception carrying the report.
- **SQLite in every CI run, Postgres on a schedule.** Rejected: a rendering that was never executed is an unreviewed file in the repo, and the two dialects differ where it matters (a table rebuild against a staged constraint).
- **`up()` in every application test.** Rejected: linear in the chain's length and runs data steps for nothing; auto-migrate is already pinned to the planner the chain's DDL comes from.

## Consequences

- `ferro.migrations.testing` is a new public module: `harness(...)` with `apply_through`, `apply`, `revert_to`, `revert_all`, `models_at`, `round_trip`.
- ADR-0038 is amended: `drift()` and `check()` are public calls returning reports with `raise_for_problems()`.
- ADR-0040 is cross-referenced: its rejection of targets stands for the application API.
- The CLI gains `down --all`.
- The Schema Management docs group (#498) gains a seventh page, **Testing migrations**.
