---
description: "Link models with foreign-key columns, forward and reverse relations and many-to-many join tables, and load related rows."
---

# Relationships

This guide shows how to link models with foreign keys: a player holds its team's key in a column, a team lists its players, and players enter tournaments through a join table Ferro creates.

Here is a whole program. It creates the tables, adds a team and a player, then reads the relation in both directions:

```python
--8<-- "docs/examples/relationships.py:first"
```

It prints:

<!-- prints: relationships.py:first -->
```text
Rustaceans ['Ferris']
```

`ferris.team` is the `Team` this session already holds, so `await ferris.team.load()` returns it without a query. `rustaceans.players` is a query that runs when you call a terminal such as `.all()`. Every example on this page runs inside the session this program opens; see [Sessions](connections.md#sessions) for why and how to open one.

Every example on this page is followed by the SQL it sends, captured from Ferro when the example runs. The two creates and the roster read send these statements. Each single-row `INSERT` asks for the new `id` with `RETURNING`:

=== "SQLite"

    ```sql
    -- relationships.py:first (sqlite)
    INSERT INTO "team" ("name")
    VALUES (?)
    RETURNING "id";

    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES (?, ?, ?)
    RETURNING "id";

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

    SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
    FROM "player"
    WHERE "player"."team_id" = $1;
    ```

`rustaceans.players.all()` is a `SELECT` on `player` filtered by `team_id`; reading `ferris.team` sent nothing.

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

Each relation names a foreign-key column. `Forward("team_id")` names a column on its own model, so `player.team` is the `Team` that `player.team_id` points at. `Reverse("team_id")` names a column on the other model, so `team.players` is a query over the players whose `team_id` holds this team's key. Neither side names the other, and a model can declare a forward relation with no reverse side. Every relation is annotated `Relation[...]`: `Relation[Team]` for a forward relation, `Relation[Team | None]` for an optional one, and `Relation[list["Player"]]` for a reverse or many-to-many side.

`create_tables()` returns a report of every statement it ran. The sections below print parts of it, so you can see the tables each relation creates.

## Foreign-key columns

A foreign-key column is a field you declare, holding the related row's primary key. `Player.team_id` is one: `Field(foreign_key=Team)` makes it reference the `team` table, and `Forward("team_id")` reads the team through it. The name is yours to choose; `team_id` is a convention.

`foreign_key=` takes the class, or its name as a string when the class comes later in the file or is the model itself (`foreign_key="Player"`). The column's type must equal the target's primary-key type, `int` here, and a mismatch is refused when the class is defined, printing the line to write. Like any column, it takes `index`, `unique`, `on_delete`, `renamed_from` and `retired`.

Read the column or filter on it without loading the team:

```python
--8<-- "docs/examples/relationships.py:foreign-key-column"
```

`ferris.team_id` is already on the instance, and the count runs one query with no join. It sends this; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:foreign-key-column (sqlite)
SELECT COUNT(*)
FROM "player"
WHERE "player"."team_id" = ?;
```

The `player` table shows its two foreign-key columns and their constraints:

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

## One-to-many

`Player.team` is a forward relation over `Player.team_id`, and `Team.players` is its reverse side, `Reverse("team_id")`. Add two more players, then read one team's roster in name order:

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

The reverse relation is a filter on the foreign-key column, `team_id`.

### Loaded relations

A forward relation is in one of two states: loaded, holding the whole related row, or unloaded, holding only its key. When a query will read a relation, ask for it with `include()`, and the related rows arrive in the same statement, loaded:

```python
--8<-- "docs/examples/relationships.py:loaded"
```

It prints:

<!-- prints: relationships.py:loaded -->
```text
Corro | Rustaceans
Ferris | Rustaceans
Gordon | Gophers
```

`player.team` is the `Team` instance itself: reading it runs no query. `include()` loads forward relations only; read a reverse or many-to-many side with `await rustaceans.players.all()`. [Loading relations with `include()`](queries.md#loading-relations-with-include) covers nullable relations, multi-hop paths and how long a relation stays loaded.

The query sends this; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:loaded (sqlite)
SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id", "j1_team"."id" AS "j1_team__id", "j1_team"."name" AS "j1_team__name"
FROM "player"
LEFT JOIN "team" AS "j1_team" ON "player"."team_id" = "j1_team"."id"
WHERE TRUE
ORDER BY "player"."name" ASC NULLS LAST;
```

`include()` added a `LEFT JOIN` on `team`, whose columns come back under `j1_team__` names.

A forward relation nothing loaded reads as an *unloaded instance*: a real `Team` holding only its key. Its key reads with no query, any other column is refused, and `await player.team.load()` fills that same object in place and returns it. A new session shows it, since the session this page runs in already holds the Rustaceans:

```python
--8<-- "docs/examples/relationships.py:unloaded"
```

It prints:

<!-- prints: relationships.py:unloaded -->
```text
Team(id=1, unloaded) 1
InstanceNotLoaded: Team(id=1) is not loaded. Read it after include(lambda player: player.team) or await player.team.load()
True Rustaceans
```

Reading `fetched.team` and its `id` sent nothing. Reading `name` raised `InstanceNotLoaded`, whose message names both ways to load the team, and `.load()` filled the object the relation already held, so `team is fetched.team`. The fetch and the load send these statements; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:unloaded (sqlite)
SELECT "player"."id", "player"."mentor_id", "player"."name", "player"."team_id"
FROM "player"
WHERE "player"."id" = ?
LIMIT ?;

SELECT "team"."id", "team"."name"
FROM "team"
WHERE "team"."id" = ?
LIMIT ?;
```

The second `SELECT` is the one `.load()` sent, by the key the unloaded instance held. Inside one session the unloaded instance is the row's one object, so any later fetch of that team fills it too; [Identity map](../concepts/identity-map.md#the-unloaded-instance) shows it.

The [`Field`][ferro.Field], [`Forward`][ferro.Forward], [`Reverse`][ferro.Reverse], [`ManyToMany`][ferro.ManyToMany] and [`Relation`][ferro.Relation] reference lists every parameter.

## One-to-one

Add `unique=True` to a foreign-key column to allow at most one child per parent. `Profile.player_id` is unique, so `Player.profile`, its reverse side `Reverse("player_id")`, holds at most one row. Like every reverse relation it is a query, annotated `Relation[list["Profile"]]`, and `.first()` reads that row or `None`:

```python
--8<-- "docs/examples/relationships.py:one-to-one"
```

Ferris's profile comes back as one `Profile`, and `assert profile is not None` tells a type checker so before `profile.bio` is read. Corro has none, so `await corro.profile.first()` returns `None`. Ferro backs `unique=True` with a unique index on the foreign-key column, `uq_profile_player_id`, so a second profile for Ferris would fail. That index also serves lookups. `index=True` beside it adds a second index, `idx_profile_player_id`, that only costs writes, so leave it out.

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

Declare [`ManyToMany(through="player_tournaments")`][ferro.ManyToMany] on one side and `Reverse("tournaments")`, naming that attribute, on the other. `Player.tournaments` and `Tournament.players` are the two sides. Enter players in tournaments, then read both directions:

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

`remove()` drops one of Ferris's links and `clear()` drops the rest. The tournaments themselves stay, and so does Gordon's entry. `add()` and `remove()` identify each instance by its primary key, whatever the field is called, so every instance must be saved first.

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

Ferro creates the join table itself, under the name `through=` gives it, here `player_tournaments`, with one `<table>_id` column for each side. The name comes from `through=` alone, so renaming the model or the attribute leaves the table as it is. Print its statements from the `create_tables()` report:

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
| `through` | required | The join table's name. Ferro creates and owns the table under this name. |
| `renamed_from` | `None` | The join table's previous name, so a migration renames the table instead of dropping it and creating a new one. |
| `reverse_index` | `True` | Adds the index on `(target_col, source_col)`, so lookups from the other side use an index. Pass `False` to skip it on a write-heavy join table. |

`reverse_index` belongs on `ManyToMany(...)`; passing it to `Reverse()` raises `TypeError`. The [`ManyToMany` reference][ferro.ManyToMany] lists every parameter.

## Self-referential relations

A model can point at itself, for mentors, org charts, threads or category trees. `Player.mentor_id` points at `Player`. A class cannot name itself while it is being defined, so `foreign_key=` takes the string `"Player"` and the relation is annotated with the string `"Player | None"`. The column is optional because its annotation is `int | None`:

```python
--8<-- "docs/examples/relationships.py:self-referential"
```

Crabby's mentor is Ferris, and Ferris's mentees are Crabby. Ferris has no mentor, which `mentor_id: int | None` allows. `crabby.mentor` is typed `Player | None`, so the block checks it is not `None` before loading it.

The create and the mentees read send these statements. Each single-row `INSERT` asks for the new `id` with `RETURNING`:

=== "SQLite"

    ```sql
    -- relationships.py:self-referential (sqlite)
    INSERT INTO "player" ("name", "team_id", "mentor_id")
    VALUES (?, ?, ?)
    RETURNING "id";

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
    WHERE "player"."mentor_id" = $1;
    ```

`crabby.mentor` is the `ferris` this session holds, so `await mentor.load()` sends nothing, and `ferris.mentees` filters `player` on `mentor_id`.

## Nullable relations

A relation is optional when its foreign-key column is. `Tournament.winner_id` is `int | None` with a `None` default, and `Tournament.winner` is annotated `Relation[Team | None]`, so a tournament can exist before anyone wins it. Nullability comes from the column's annotation alone:

```python
--8<-- "docs/examples/relationships.py:nullable"
```

The Autumn Cup has no winner, so its `winner_id` is `None`. Setting `winner_id` and saving records the Rustaceans' Spring Open win, and `rustaceans.titles` finds it.

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

`on_delete="SET NULL"` needs an optional column. On a required one it is refused when the class is defined, printing the optional declaration to write.

## Assigning a relation

Assigning a forward relation sets its foreign-key column. Move Ferris to the Gophers and back, clear Crabby's mentor, then try three assignments Ferro refuses:

```python
--8<-- "docs/examples/relationships.py:assign"
```

It prints:

<!-- prints: relationships.py:assign -->
```text
True True
Rustaceans
None
TypeError: Player.team: expected a Team, got Tournament. Assign a saved Team instance, or set team_id directly
ValueError: Player.team: the Team has no id yet. Save it first, then assign it
TypeError: Player.team: team_id is not nullable, so the relation cannot be None. Assign a Team, or make both nullable. Fix it by hand: (1) on Player, replace team_id with `team_id: int | None = Field(default=None, foreign_key=Team, index=True)`; (2) on Player, replace team with `team: Relation[Team | None] = Forward("team_id")`
```

`ferris.team = gophers` set `team_id` to the Gophers' id and kept `gophers` as the loaded relation, so reading `ferris.team` needs no query. Setting `team_id` directly dropped that loaded relation: `ferris.team` then read as the Rustaceans this session holds. `crabby.mentor = None` cleared the optional key. Each refusal names its fix: a relation takes an instance of its own model, saved so it has a key, and `None` only when its column is optional. A type checker reports the first and third before the code runs, which is why the block types `wrong` as `Any`.

The two saves send these statements; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:assign (sqlite)
UPDATE "player"
SET "name" = ?, "team_id" = ?, "mentor_id" = ?
WHERE "id" = ?;

UPDATE "player"
SET "name" = ?, "team_id" = ?, "mentor_id" = ?
WHERE "id" = ?;
```

Each `UPDATE` writes `team_id` with the rest of the row. Assigning sent nothing until `save()`.

## Building with a related instance

A new row takes the related instance's key. A primary key declared `id: int | None = Field(default=None, primary_key=True)` is `None` until the row is saved, and a type checker reads the declared type, not the saved state. So in a constructor it reports `team_id=rustaceans.id` as `int | None` where `int` is required. There are three ways to write it that type-check clean:

```python
--8<-- "docs/examples/relationships.py:build"
```

Rusty, the Winter Cup and Oxide are each saved a different way:

- `await Player.create(name="Rusty", team_id=rustaceans.id)` passes the key as it is. A type checker does not check `create()`'s arguments today, so it reports nothing here, right or wrong.
- `winter.winner = gophers` builds the row first and assigns the relation. It suits an optional key, which the constructor can leave out; a required key cannot be left out.
- `assert rustaceans.id is not None` narrows the id to `int`, after which the constructor `Player(name="Oxide", team_id=rustaceans.id)` type-checks.

The three writes send these statements; on PostgreSQL only the placeholders differ:

```sql
-- relationships.py:build (sqlite)
INSERT INTO "player" ("name", "team_id", "mentor_id")
VALUES (?, ?, ?)
RETURNING "id";

INSERT INTO "tournament" ("name", "winner_id")
VALUES (?, ?)
RETURNING "id";

INSERT INTO "player" ("name", "team_id", "mentor_id")
VALUES (?, ?, ?)
RETURNING "id";
```

Each is one `INSERT`, the key written like any other column.

## Delete behavior

`on_delete=` on the foreign-key column decides what happens to child rows when their parent row is deleted:

| `on_delete` | When the parent row is deleted |
| :--- | :--- |
| `"CASCADE"` (default) | The child rows are deleted too. |
| `"RESTRICT"` | The delete fails with `ForeignKeyViolationError` while child rows exist. |
| `"SET NULL"` | The foreign-key column is set to `NULL`; the column must be optional. |
| `"SET DEFAULT"` | The foreign-key column is reset to its column default. |
| `"NO ACTION"` | Like `RESTRICT`, the delete fails while child rows exist; the database checks at the end of the statement rather than at once. |

Delete the Rustaceans, and each rule in the roster runs:

```python
--8<-- "docs/examples/relationships.py:on-delete"
```

Corro, Ferris, Crabby, Rusty and Oxide go with their team (`CASCADE` on `Player.team_id`), and Ferris's profile goes with Ferris (`CASCADE` on `Profile.player_id`). The Spring Open stays, with no winner (`SET NULL` on `Tournament.winner_id`). Gordon plays for the Gophers and stays. Had only Ferris been deleted, `SET NULL` on `Player.mentor_id` would have kept Crabby, with no mentor.

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

PostgreSQL does not index foreign-key columns on its own. For a foreign key on a busy query path, such as a tenant id that every list filters on, pass `index=True`. `Player.team_id` does, as `Field(foreign_key=Team, index=True)`, and that is where `idx_player_team_id` in the [`player` table](#foreign-key-columns) comes from.

A one-to-one relation already has its unique index. For an index over several columns that starts with the foreign key, use [composite indexes](models-and-fields.md#composite-indexes).

## See also

- [Field, Forward, Reverse, ManyToMany and Relation](../api/relationships.md): the reference for every parameter
- [Models and fields](models-and-fields.md): field declaration styles and constraints
- [Queries](queries.md): filtering on foreign-key columns, traversing relations and `include()`
- [Mutations](mutations.md): creating related rows and what a delete cascades to
- [Schema management](schema/overview.md): how foreign keys and join tables reach the database, through auto-migrate, [migrations](schema/migrations.md) or the [Alembic bridge](schema/alembic.md)
