---
description: "API reference for ForeignKey, BackRef, ManyToMany and Relation."
---

# ForeignKey, BackRef, ManyToMany and Relation

Relationships are declared with annotations: `Annotated[Other, ForeignKey(...)]` for the owning side, `BackRef()` for the reverse side, and `ManyToMany(...)` for join-table relations. At runtime you reach related rows through `Relation`, an awaitable, chainable query bound to the instance. See the [Relationships guide](../guide/relationships.md) for usage patterns.

::: ferro.ForeignKey

::: ferro.BackRef

::: ferro.ManyToMany

::: ferro.Relation
    options:
      inherited_members: [add, remove, clear]
