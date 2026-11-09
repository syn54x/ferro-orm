# A relation's name never reaches the schema

```python
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team)                    # column "team_id"
    team: Relation[Team] = Forward("team_id")                 # no column
    tournaments: Relation[list["Tournament"]] = ManyToMany(through="player_tournaments")
                                                              # join table "player_tournaments"
```

Nothing in the schema comes from a relation's name. A foreign-key column is the column you declare, under the name you give it, and a relation reads that column without adding one. A many-to-many's join table is the name you pass to `through=`, which is required. Renaming `team` or `tournaments` changes no table, column, index or constraint, and `ferro migrate new` writes nothing for it.

Until now the names came from the relation. `team: Annotated[Team, ForeignKey(...)]` created a `team_id` column, so renaming the relation renamed the column (or needed `ForeignKey(renamed_from=...)` to keep it). `tournaments = ManyToMany(related_name="players")` created a join table named `player_tournaments`, the owning table and the attribute, so renaming the attribute renamed a table full of rows. An edit that reads as a Python refactor was a schema migration.

Ferro still creates and owns the join table: its two `<table>_id` columns, the unique index over `(source, target)` and the index over `(target, source)` unless `reverse_index=False`. Only its name moves from derived to declared. A conversion writes the name the old form derived, so the schema stays byte for byte the same:

```python
tournaments: Relation[list["Tournament"]] = ManyToMany(related_name="players")
# converts to
tournaments: Relation[list["Tournament"]] = ManyToMany(through="player_tournaments")
```

A `ManyToMany` with no `through=` is refused when its class is defined, and the refusal prints that derived name: `Player.tournaments: a many-to-many needs through=, the name of its join table. Write: tournaments: Relation[list["Tournament"]] = ManyToMany(through="player_tournaments")`.

Decided in Epic #874 (2026-11-07), built in #876 for foreign-key columns and #877 for join tables.

## Considered options

- **Derive the join table's name from the owning table and the attribute (`<table>_<field>`).** This was the old default. It saved writing one name, but it renamed a table whenever an attribute was renamed, and the attribute's name is the one a developer is most likely to change while tidying a model. A rename the developer did not mean as a schema change would drop a table and create an empty one, or need a rename hint on a relation to stop it. Rejected.
- **Keep `through=` optional, with the derived name as its default.** The conversion would be shorter, but the trap above stays for every new model, and the schema would have two sources of names, one declared and one implied. Rejected.
- **Derive the name from the two tables (`player_tournament`).** Stable under an attribute rename, but two many-to-many relations between the same models would collide, and every existing join table would be renamed by the conversion. Rejected.

## Consequences

- A model converted from the old form keeps its join table and its key columns, and `ferro migrate new` over a snapshot recorded from the old form prints `no schema change: nothing written`. The pin is `tests/test_many_to_many_through.py`, against the snapshot in `tests/fixtures/many_to_many_pre_conversion/`.
- A table rename on the owning model (ADR-0032) still renames the join table's `<table>_id` column, which is derived from the table, but no longer the join table itself, since its name is declared. The table keeps the name `through=` gives it.
- A join table is renamed on purpose with `ManyToMany(through="author_tags", renamed_from="writer_tags")` (#907): `renamed_from` is the join table's rename hint, so every emitter renames the table and the names derived from it and keeps its rows. Changing `through=` with no `renamed_from` is a new table and a dropped one.
- The join table's two columns are still named after the two tables. A self-referential many-to-many, whose two columns would share a name, stays out of scope: `ManyToMany(through=...)` aimed at its own model is refused when relations resolve, naming a join model with two foreign-key columns as the way to declare one.
- `related_name` and `BackRef` leave the API with the old form; the other side of a many-to-many is a `Reverse` naming the owning attribute, and it may be left out.
