---
title: Foreign-key columns
type: pattern
tags: [convention, schema, relationships]
related_files:
  - src/ferro/base.py
  - src/ferro/fields.py
  - src/ferro/metaclass.py
  - src/ferro/columns.py
  - src/ferro/relations/__init__.py
related_issues: [874, 887, 936]
captured: 2026-11-09
---

## Problem

A relation needs a column that holds the related row's primary key, and every
column-level concern (an index, a unique constraint, an `ON DELETE` action)
needs a place to live.

## Takeaway

The foreign-key column is a field the user declares, and every option it
takes is that field's own. A relation never adds a column; it names one.

```python
class Team(Model):
    id: int | None = Field(default=None, primary_key=True)
    players: Relation[list["Player"]] = Reverse("team_id")


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key=Team, index=True, on_delete="CASCADE")
    team: Relation[Team] = Forward("team_id")
```

`team_id` is in the table, the Pydantic model and the constructor like any
other field. `Forward("team_id")` and `Reverse("team_id")` name the column by
its attribute name, and neither adds anything to the schema.

## The rules

- **Options are the field's own.** `index`, `unique`, `on_delete`, `db_type`,
  `renamed_from` and `retired` go on the `Field(foreign_key=...)` call, as on
  any column. Nullability comes from the annotation alone
  (`team_id: int | None`).
- **The key agrees with its target.** The column's Python type, with
  `| None` set aside and a `NewType` read as its supertype, must equal the
  target primary key's type, and its storage must equal the target key's
  storage. A disagreement is refused at class definition, or at relation
  resolution for a string target, naming the line or the `db_type` that fixes
  it. The check is `check_key_column` in `src/ferro/relations/__init__.py`.
- **A relation names a column.** `Forward("<column>")` names a key column on
  its own model; `Reverse("<column>")` names one on the model in its
  annotation. A relation over a column with no `foreign_key` is refused.
- **Join tables are Ferro's.** A `ManyToMany` still gets a join table whose
  two `<table>_id` columns Ferro builds with `fk_column_spec` in
  `src/ferro/columns.py`, each derived from the column spec of its side's
  primary key: the type and storage (`db_type` included) are the key's, so
  a `Decimal` key gets a `numeric` join column and a `varchar(36)` key a
  `varchar(36)` one. A join column meets the rule a declared key column is
  checked against by construction. A side with no primary key is refused
  when relations resolve, its fix the default
  `id: int | None = Field(default=None, primary_key=True)`.

## Recipe: test a column option

Read the option off the compiled table by the column's own name:

```python
table = get_metadata().tables["player"]
assert table.c.team_id.nullable is False
assert "idx_player_team_id" in {index.name for index in table.indexes}
```

`table.c.team` raises `AttributeError` (and `table.c["team"]` a `KeyError`):
a relation's name reaches no schema artifact.

## How to recognize

- A schema, snapshot or DDL line that names a relation (`team`) where its
  column (`team_id`) belongs is a bug: a relation's name is never a column.
- An option that seems to belong "on the relation" belongs on the
  `Field(foreign_key=...)` declaration of the column the relation names.
