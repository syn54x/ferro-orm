# Relationships

Relationships are declared with annotations: `Annotated[Other, ForeignKey(...)]` for the owning side, `BackRef()` for the reverse side, and `ManyToMany(...)` for join-table relations. At runtime, related data is accessed through `Relation` — an awaitable, chainable query bound to the instance. See the [Relationships guide](../guide/relationships.md) for usage patterns.

::: ferro.ForeignKey

::: ferro.BackRef

::: ferro.ManyToMany

::: ferro.Relation
