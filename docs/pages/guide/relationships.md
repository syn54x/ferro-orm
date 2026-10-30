---
description: "Link models with foreign keys, back-references and many-to-many join tables, and load related rows."
---

# Relationships

This guide shows how to link models with foreign keys: a player belongs to a team, a team lists its players, and players enter tournaments through a join table Ferro creates.

Here is a whole program. It creates the tables, adds a team and a player, then reads the relation in both directions:

```python
--8<-- "docs/examples/relationships.py:first"
```

It prints:

<!-- prints: relationships.py:first -->
```text
Rustaceans ['Ferris']
```

`await ferris.team` runs one query and returns the `Team`. `rustaceans.players` is a query that runs when you call a terminal such as `.all()`. Every example on this page runs inside the session this program opens; see [Sessions](connections.md#sessions) for why and how to open one.

Every example on this page is followed by the SQL it sends, captured from Ferro when the example runs. The two creates and the two reads send these statements. Each single-row `INSERT` asks for the new `id` with `RETURNING`:

=== "SQLite"

    ```sql
    -- relationships.py:first (sqlite)
    INSERT INTO "team" ("name")
    VALUES (?)
    RETURNING "id";

    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES (?, ?, ?)
    RETURNING "id";

    SELECT "team"."id", "team"."name"
    FROM "team"
    WHERE "team"."id" = ?
    LIMIT ?;

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."team_id" = ?;
    ```

=== "PostgreSQL"

    ```sql
    -- relationships.py:first (postgres)
    INSERT INTO "team" ("name")
    VALUES ($1)
    RETURNING "id";

    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES ($1, $2, $3)
    RETURNING "id";

    SELECT "team"."id", "team"."name"
    FROM "team"
    WHERE "team"."id" = $1
    LIMIT $2;

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."team_id" = $1;
    ```

`await ferris.team` is a `SELECT` on `team` by primary key; `rustaceans.players.all()` is a `SELECT` on `player` filtered by `team_id`.

## The roster

Every example on this page uses one roster. A team has players, a player may have a profile and a mentor, players enter tournaments, and a tournament may have a winning team:

=== "Assignment"

    ```python
    --8<-- "docs/examples/relationships.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/relationships_annotated.py:models"
    ```

Each relation has two sides. The forward side is a [`ForeignKey`][ferro.ForeignKey] field on the model whose table holds the key column. The reverse side is a [`BackRef()`][ferro.BackRef] on the other model, named by the forward side's `related_name`. Ferro checks the pairing at `connect()`: a `related_name` with no matching `BackRef()` raises `RuntimeError` naming both models.

`create_tables()` returns a report of every statement it ran. The sections below print parts of it, so you can see the tables each relation creates.

## One-to-many

`Player.team` is a `ForeignKey` to `Team`, and `Team.players` is its reverse side, a `Relation[list["Player"]]`. Add two more players, then read one team's roster in name order:

```python
--8<-- "docs/examples/relationships.py:one-to-many"
```

Gordon plays for the Gophers, so the Rustaceans' roster is Corro and Ferris. `rustaceans.players` is a query, so you can filter, order and page it before you run it.

The three creates and the roster query send these statements. Each single-row `INSERT` asks for the new `id` with `RETURNING`:

=== "SQLite"

    ```sql
    -- relationships.py:one-to-many (sqlite)
    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES (?, ?, ?)
    RETURNING "id";

    INSERT INTO "team" ("name")
    VALUES (?)
    RETURNING "id";

    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES (?, ?, ?)
    RETURNING "id";

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."team_id" = ?
    ORDER BY "player"."name" ASC NULLS LAST;
    ```

=== "PostgreSQL"

    ```sql
    -- relationships.py:one-to-many (postgres)
    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES ($1, $2, $3)
    RETURNING "id";

    INSERT INTO "team" ("name")
    VALUES ($1)
    RETURNING "id";

    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES ($1, $2, $3)
    RETURNING "id";

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."team_id" = $1
    ORDER BY "player"."name" ASC NULLS LAST;
    ```

The reverse relation is a filter on the shadow column, `team_id`.

### Shadow foreign-key columns

For each `ForeignKey` field, Ferro adds a column and a model field named after it with `_id` (`team_id` for `team`), holding the related row's primary key. Its Python type follows the target model's primary key. Read it or filter on it without loading the team:

```python
--8<-- "docs/examples/relationships.py:shadow-column"
```

`ferris.team_id` is already on the instance, and the count runs one query with no join. It sends this; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:shadow-column (sqlite)
SELECT COUNT(*)
FROM "player"
WHERE "player"."team_id" = ?;
```

The `player` table shows the shadow columns and their constraints:

```python
--8<-- "docs/examples/relationships.py:player-ddl"
```

It prints these statements, laid out one column per line. PostgreSQL declares `id` as `serial` and runs the statements under a lock timeout:

=== "SQLite"

    ```sql
    -- relationships.py:player-ddl (sqlite)
    CREATE TABLE IF NOT EXISTS "player" (
      "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
      "mentor_id" integer,
      "name" varchar NOT NULL,
      "team_id" integer NOT NULL,
      CONSTRAINT "fk_player_mentor_id_player" FOREIGN KEY ("mentor_id") REFERENCES "player" ("id") ON DELETE SET NULL,
      CONSTRAINT "fk_player_team_id_team" FOREIGN KEY ("team_id") REFERENCES "team" ("id") ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS "idx_player_team_id" ON "player" ("team_id");
    ```

=== "PostgreSQL"

    ```sql
    -- relationships.py:player-ddl (postgres)
    SET LOCAL lock_timeout = '5000ms';

    CREATE TABLE IF NOT EXISTS "player" (
      "id" serial PRIMARY KEY NOT NULL,
      "mentor_id" integer,
      "name" varchar NOT NULL,
      "team_id" integer NOT NULL,
      CONSTRAINT "fk_player_mentor_id_player" FOREIGN KEY ("mentor_id") REFERENCES "player" ("id") ON DELETE SET NULL,
      CONSTRAINT "fk_player_team_id_team" FOREIGN KEY ("team_id") REFERENCES "team" ("id") ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS "idx_player_team_id" ON "player" ("team_id");
    ```

Each foreign key becomes a constraint named `fk_<table>_<column>_<target table>`. `idx_player_team_id` comes from `index=True`; see [Indexing foreign keys](#indexing-foreign-keys).

To filter or order by a column of the related model, a lambda can follow the relation, as in `Player.where(lambda player: player.team.name == "Rustaceans")`. [Querying across relationships](queries.md#querying-across-relationships) covers it.

### Populated relations

Awaiting a forward relation in a loop runs one query per row. When a query will read a relation, ask for it with `include()`, and the related rows arrive in the same statement:

```python
--8<-- "docs/examples/relationships.py:populated"
```

It prints:

<!-- prints: relationships.py:populated -->
```text
Corro | Rustaceans
Ferris | Rustaceans
Gordon | Gophers
```

`player.team` is now the `Team` instance itself: reading it needs no `await` and runs no query. `include()` covers forward foreign keys only; load a `BackRef` or many-to-many collection with `await rustaceans.players.all()`. [Populating relations with `include()`](queries.md#populating-relations-with-include) covers nullable relations, multi-hop paths and how long a relation stays populated.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:populated (sqlite)
SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id", "j1_team"."id" AS "j1_team__id", "j1_team"."name" AS "j1_team__name"
FROM "player"
LEFT JOIN "team" AS "j1_team" ON "player"."team_id" = "j1_team"."id"
WHERE TRUE
ORDER BY "player"."name" ASC NULLS LAST;
```

`include()` added a `LEFT JOIN` on `team`, whose columns come back under `j1_team__` names.

The [`ForeignKey`][ferro.ForeignKey], [`BackRef`][ferro.BackRef] and [`Relation`][ferro.Relation] reference lists every parameter.

## One-to-one

Add `unique=True` to a `ForeignKey` to allow at most one child per parent. `Profile.player` is unique, so its reverse side `Player.profile` is a `BackRef()` annotated with the single model, `"Profile"`, and awaiting it returns one instance or `None`:

```python
--8<-- "docs/examples/relationships.py:one-to-one"
```

Ferris's profile comes back as one `Profile`. Corro has none, so `await corro.profile` returns `None`. Ferro backs `unique=True` with a unique index on the shadow column, `uq_profile_player_id`, so a second profile for Ferris would fail. That index also serves lookups, so `index=True` next to `unique=True` is redundant, and Ferro warns with a `UserWarning` and ignores it.

The create and the two reads send these statements. Each single-row `INSERT` asks for the new `id` with `RETURNING`:

=== "SQLite"

    ```sql
    -- relationships.py:one-to-one (sqlite)
    INSERT INTO "profile" ("bio", "player_id")
    VALUES (?, ?)
    RETURNING "id";

    SELECT "profile"."bio", "profile"."id", "profile"."player_id"
    FROM "profile"
    WHERE "profile"."player_id" = ?
    LIMIT ?;

    SELECT "profile"."bio", "profile"."id", "profile"."player_id"
    FROM "profile"
    WHERE "profile"."player_id" = ?
    LIMIT ?;
    ```

=== "PostgreSQL"

    ```sql
    -- relationships.py:one-to-one (postgres)
    INSERT INTO "profile" ("bio", "player_id")
    VALUES ($1, $2)
    RETURNING "id";

    SELECT "profile"."bio", "profile"."id", "profile"."player_id"
    FROM "profile"
    WHERE "profile"."player_id" = $1
    LIMIT $2;

    SELECT "profile"."bio", "profile"."id", "profile"."player_id"
    FROM "profile"
    WHERE "profile"."player_id" = $1
    LIMIT $2;
    ```

Each read of the one-to-one side is a `SELECT` on `profile` by `player_id` with a `LIMIT` of 1.

## Many-to-many

Declare [`ManyToMany(related_name=...)`][ferro.ManyToMany] on one side and a `BackRef()` collection on the other. `Player.tournaments` and `Tournament.players` are the two sides. Enter players in tournaments, then read both directions:

```python
--8<-- "docs/examples/relationships.py:many-to-many"
```

Ferris entered both tournaments, and the Spring Open has Ferris and Gordon. The creates, the links and the two reads send these statements. Each single-row `INSERT` asks for the new `id` with `RETURNING`:

=== "SQLite"

    ```sql
    -- relationships.py:many-to-many (sqlite)
    INSERT INTO "tournament" ("name", "winner_id")
    VALUES (?, ?)
    RETURNING "id";

    INSERT INTO "tournament" ("name", "winner_id")
    VALUES (?, ?)
    RETURNING "id";

    INSERT INTO "player_tournaments" ("player_id", "tournament_id")
    VALUES (?, ?), (?, ?);

    INSERT INTO "player_tournaments" ("player_id", "tournament_id")
    VALUES (?, ?);

    SELECT "tournament"."id", "tournament"."name", "tournament"."winner_id"
    FROM "tournament"
    INNER JOIN "player_tournaments" ON "tournament"."id" = "player_tournaments"."tournament_id"
    WHERE "player_tournaments"."player_id" = ?
    ORDER BY "tournament"."name" ASC NULLS LAST;

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    INNER JOIN "player_tournaments" ON "player"."id" = "player_tournaments"."player_id"
    WHERE "player_tournaments"."tournament_id" = ?
    ORDER BY "player"."name" ASC NULLS LAST;
    ```

=== "PostgreSQL"

    ```sql
    -- relationships.py:many-to-many (postgres)
    INSERT INTO "tournament" ("name", "winner_id")
    VALUES ($1, $2)
    RETURNING "id";

    INSERT INTO "tournament" ("name", "winner_id")
    VALUES ($1, $2)
    RETURNING "id";

    INSERT INTO "player_tournaments" ("player_id", "tournament_id")
    VALUES ($1, $2), ($3, $4);

    INSERT INTO "player_tournaments" ("player_id", "tournament_id")
    VALUES ($1, $2);

    SELECT "tournament"."id", "tournament"."name", "tournament"."winner_id"
    FROM "tournament"
    INNER JOIN "player_tournaments" ON "tournament"."id" = "player_tournaments"."tournament_id"
    WHERE "player_tournaments"."player_id" = $1
    ORDER BY "tournament"."name" ASC NULLS LAST;

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    INNER JOIN "player_tournaments" ON "player"."id" = "player_tournaments"."player_id"
    WHERE "player_tournaments"."tournament_id" = $1
    ORDER BY "player"."name" ASC NULLS LAST;
    ```

Each `add()` is one `INSERT` into the join table, and each side reads through it with an `INNER JOIN`. Both sides are queries with three link methods, `add(*instances)`, `remove(*instances)` and `clear()`:

```python
--8<-- "docs/examples/relationships.py:m2m-remove"
```

`remove()` drops one of Ferris's links and `clear()` drops the rest. The tournaments themselves stay, and so does Gordon's entry. `add()` and `remove()` read each instance's `id` attribute, so they work only when the related model's primary key is named `id`.

The calls send these statements; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:m2m-remove (sqlite)
DELETE
FROM "player_tournaments"
WHERE "player_id" = ? AND "tournament_id" IN (?);

SELECT "tournament"."id", "tournament"."name", "tournament"."winner_id"
FROM "tournament"
INNER JOIN "player_tournaments" ON "tournament"."id" = "player_tournaments"."tournament_id"
WHERE "player_tournaments"."player_id" = ?;

DELETE
FROM "player_tournaments"
WHERE "player_id" = ?;

SELECT COUNT(*)
FROM "tournament"
INNER JOIN "player_tournaments" ON "tournament"."id" = "player_tournaments"."tournament_id"
WHERE "player_tournaments"."player_id" = ?;

SELECT COUNT(*)
FROM "player"
INNER JOIN "player_tournaments" ON "player"."id" = "player_tournaments"."player_id"
WHERE "player_tournaments"."tournament_id" = ?;
```

`remove()` and `clear()` delete rows of the join table only.

### The join table

Ferro creates the join table itself. It names the table after the side that declares `ManyToMany`, as `<table>_<field>`, here `player_tournaments`, with one `<table>_id` column for each side. Print its statements from the `create_tables()` report:

```python
--8<-- "docs/examples/relationships.py:join-table-ddl"
```

It prints these statements, laid out one column per line. PostgreSQL runs them under a lock timeout:

=== "SQLite"

    ```sql
    -- relationships.py:join-table-ddl (sqlite)
    CREATE TABLE IF NOT EXISTS "player_tournaments" (
      "player_id" integer NOT NULL,
      "tournament_id" integer NOT NULL,
      CONSTRAINT "fk_player_tournaments_player_id_player" FOREIGN KEY ("player_id") REFERENCES "player" ("id") ON DELETE CASCADE,
      CONSTRAINT "fk_player_tournaments_tournament_id_tournament" FOREIGN KEY ("tournament_id") REFERENCES "tournament" ("id") ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS "idx_player_tournaments_tournament_id_player_id" ON "player_tournaments" ("tournament_id", "player_id");

    CREATE UNIQUE INDEX IF NOT EXISTS "uq_player_tournaments_player_id_tournament_id" ON "player_tournaments" ("player_id", "tournament_id");
    ```

=== "PostgreSQL"

    ```sql
    -- relationships.py:join-table-ddl (postgres)
    SET LOCAL lock_timeout = '5000ms';

    CREATE TABLE IF NOT EXISTS "player_tournaments" (
      "player_id" integer NOT NULL,
      "tournament_id" integer NOT NULL,
      CONSTRAINT "fk_player_tournaments_player_id_player" FOREIGN KEY ("player_id") REFERENCES "player" ("id") ON DELETE CASCADE,
      CONSTRAINT "fk_player_tournaments_tournament_id_tournament" FOREIGN KEY ("tournament_id") REFERENCES "tournament" ("id") ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS "idx_player_tournaments_tournament_id_player_id" ON "player_tournaments" ("tournament_id", "player_id");

    CREATE UNIQUE INDEX IF NOT EXISTS "uq_player_tournaments_player_id_tournament_id" ON "player_tournaments" ("player_id", "tournament_id");
    ```

Both link columns cascade, so deleting a player or a tournament deletes its links. The unique index stores each link once, and the index on `(tournament_id, player_id)` serves lookups from the tournament side.

`ManyToMany` takes three parameters:

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `related_name` | required | Name of the `BackRef()` field on the related model. |
| `through` | `None` | Join-table name; `<table>_<field>` when omitted. |
| `reverse_index` | `True` | Adds the index on `(target_col, source_col)`, so lookups from the other side use an index. Pass `False` to skip it on a write-heavy join table. |

`reverse_index` belongs on `ManyToMany(...)`; passing it to `BackRef()` raises `TypeError`. The [`ManyToMany` reference][ferro.ManyToMany] lists every parameter.

## Self-referential relations

A model can point at itself, for mentors, org charts, threads or category trees. `Player.mentor` is a `ForeignKey` to `Player`. A class cannot name itself in its own annotations, so the target is the string `"Player"`, and because that string cannot carry `| None`, `nullable=True` makes the column optional:

```python
--8<-- "docs/examples/relationships.py:self-referential"
```

Crabby's mentor is Ferris, and Ferris's mentees are Crabby. Ferris has no mentor, which the `mentor_id` column allows only because of `nullable=True`. Writing `"Player | None"` instead fails at `connect()` with `RuntimeError: Relationship resolution failed: 'Player | None' not found`.

The create and the two reads send these statements. Each single-row `INSERT` asks for the new `id` with `RETURNING`:

=== "SQLite"

    ```sql
    -- relationships.py:self-referential (sqlite)
    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES (?, ?, ?)
    RETURNING "id";

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."id" = ?
    LIMIT ?;

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."mentor_id" = ?;
    ```

=== "PostgreSQL"

    ```sql
    -- relationships.py:self-referential (postgres)
    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES ($1, $2, $3)
    RETURNING "id";

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."id" = $1
    LIMIT $2;

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."mentor_id" = $1;
    ```

`await crabby.mentor` reads `player` by primary key, and `ferris.mentees` filters `player` on `mentor_id`.

## Nullable relations

For an optional relation to another model, declare it `Annotated[Team | None, ForeignKey(...)] = None` (`Annotated[Team, ForeignKey(...)] | None` and `Optional[...]` mean the same) and default the field to `None`. `Tournament.winner` is the first form, so a tournament can exist before anyone wins it:

```python
--8<-- "docs/examples/relationships.py:nullable"
```

The Autumn Cup has no winner, so its `winner_id` is `None`. Setting `winner_id` and saving records the Rustaceans' Spring Open win, and `rustaceans.titles` finds it. A relation field cannot be assigned on an instance (`spring.winner = rustaceans` raises `AttributeError`), so set the shadow column.

The save and the query send these statements; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:nullable (sqlite)
UPDATE "tournament"
SET "name" = ?, "winner_id" = ?
WHERE "id" = ?;

SELECT "tournament"."id", "tournament"."name", "tournament"."winner_id"
FROM "tournament"
WHERE "tournament"."winner_id" = ?;
```

`save()` wrote `winner_id` with the rest of the row, and `rustaceans.titles` filters on it.

With the default `nullable="infer"`, the column's nullability follows the annotation. `on_delete="SET NULL"` needs a nullable column, so combining it with `nullable=False` raises `ValueError`.

## Delete behavior

`ForeignKey(on_delete=...)` decides what happens to child rows when their parent row is deleted:

| `on_delete` | When the parent row is deleted |
| :--- | :--- |
| `"CASCADE"` (default) | The child rows are deleted too. |
| `"RESTRICT"` | The delete fails with `ForeignKeyViolationError` while child rows exist. |
| `"SET NULL"` | The shadow column is set to `NULL`; the relation must be nullable. |
| `"SET DEFAULT"` | The shadow column is reset to its column default. |
| `"NO ACTION"` | Like `RESTRICT`, the delete fails while child rows exist; the database checks at the end of the statement rather than at once. |

Delete the Rustaceans, and each rule in the roster runs:

```python
--8<-- "docs/examples/relationships.py:on-delete"
```

Corro, Ferris and Crabby go with their team (`CASCADE` on `Player.team`), and Ferris's profile goes with Ferris (`CASCADE` on `Profile.player`). The Spring Open stays, with no winner (`SET NULL` on `Tournament.winner`). Gordon plays for the Gophers and stays. Had only Ferris been deleted, `SET NULL` on `Player.mentor` would have kept Crabby, with no mentor.

The delete and the checks send these statements; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:on-delete (sqlite)
DELETE
FROM "team"
WHERE "team"."id" = ?;

SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
FROM "player"
WHERE TRUE;

SELECT COUNT(*)
FROM "profile"
WHERE TRUE;

SELECT "tournament"."id", "tournament"."name", "tournament"."winner_id"
FROM "tournament"
WHERE "tournament"."id" = ?
LIMIT ?;
```

Ferro sends one `DELETE`; the database applies each `on_delete` rule itself.

Because `CASCADE` is the default, deleting a parent removes its children unless you choose otherwise. Pick `RESTRICT` when deleting a parent that still has children would be a bug you want to surface.

## Indexing foreign keys

PostgreSQL does not index foreign-key columns on its own. For a foreign key on a busy query path, such as a tenant id that every list filters on, pass `index=True`. `Player.team` does, as `ForeignKey(related_name="players", index=True)`, and that is where `idx_player_team_id` in the [`player` table](#shadow-foreign-key-columns) comes from.

A one-to-one relation already has its unique index. For an index over several columns that starts with the foreign key, use [composite indexes](models-and-fields.md#composite-indexes).

## See also

- [Relationships reference](../api/relationships.md): `ForeignKey`, `BackRef`, `ManyToMany` and `Relation`
- [Models and fields](models-and-fields.md): field declaration styles and constraints
- [Queries](queries.md): filtering on shadow columns, traversing relations and `include()`
- [Mutations](mutations.md): creating related rows and what a delete cascades to
- [Schema management](schema/overview.md): how foreign keys and join tables reach the database, through auto-migrate, [migrations](schema/migrations.md) or the [Alembic bridge](schema/alembic.md)
