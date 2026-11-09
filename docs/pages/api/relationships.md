---
description: "API reference for Field, Forward, Reverse, ManyToMany and Relation."
---

# Field, Forward, Reverse, ManyToMany and Relation

A relationship starts from a foreign-key column, declared as `team_id: int = Field(foreign_key=Team)`; [`Field`][ferro.Field] is documented with the other field options. `Forward("team_id")` reads the related instance through that column, `Reverse("team_id")` queries the rows of another model whose column points back, and `ManyToMany(through=...)` links two models through a join table Ferro creates. Every relation is annotated [`Relation`][ferro.Relation]. See the [Relationships guide](../guide/relationships.md) for usage patterns.

::: ferro.ManyToMany

::: ferro.Forward

::: ferro.Reverse

::: ferro.Relation
    options:
      inherited_members: [add, remove, clear]
