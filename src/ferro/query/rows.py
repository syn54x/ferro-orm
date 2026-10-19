"""The records a `select()` projection returns: [`Row`][ferro.query.Row] and [`Rows`][ferro.query.Rows].

A projection returns records, never model instances, because a model
instance always holds a complete row. Both types behave like Pydantic models
for `model_dump()` and as a FastAPI `response_model`.
"""

# Both are built without Pydantic validation: rows come off the same
# direct-to-dict hydration path as model instances (AGENTS.md I-2), and
# ``Rows`` wraps them without re-validating.

from collections.abc import Iterator
from typing import TypeVar, overload

from pydantic import BaseModel, ConfigDict, RootModel

R = TypeVar("R")
"""The record type a `Rows` list holds."""


class Row(BaseModel):
    """One record from a `select()` projection: the selected fields, by name.

    A `Row` is not a model instance: it has no `save()` or `refresh()`, the
    session does not track it, and its fields are the ones the projection
    selected, in that order. Reading a field, `model_dump()`,
    `model_dump_json()`, equality and `repr` work as on any Pydantic model.
    See
    [Selecting a column subset](../guide/queries.md#selecting-a-column-subset).

    Examples:
        ```python {test=run prelude=reference}
        rows = await Post.select(lambda post: (post.id, post.views)).order_by(lambda post: post.id).all()
        assert rows[0].views == 1843
        assert rows[0].model_dump() == {"id": 1, "views": 1843}
        ```
    """

    model_config = ConfigDict(extra="allow")
    """The selected fields are stored as Pydantic extra fields."""


class Rows(RootModel[list[R]]):
    """The list of records a projected query's `all()` returns.

    It works like a list: indexing, slicing (which returns a `Rows`),
    iteration, `len()` and truthiness. As a Pydantic `RootModel`,
    `model_dump()` gives a list of dicts and `Rows[Row]` works as a FastAPI
    `response_model`.

    Examples:
        ```python {test=run prelude=reference}
        rows = await Post.select(lambda post: (post.id, post.title)).order_by(lambda post: post.id).all()
        assert len(rows) == 2
        assert rows[:1].model_dump() == [{"id": 1, "title": "Notes on the Analytical Engine"}]
        ```
    """

    def __len__(self) -> int:
        """Count the records."""
        return len(self.root)

    def __iter__(self) -> Iterator[R]:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        """Iterate over the records."""
        # BaseModel.__iter__ yields (field, value) pairs; a Rows iterates its
        # records, like the list it stands in for.
        return iter(self.root)

    @overload
    def __getitem__(self, index: int) -> R: ...

    @overload
    def __getitem__(self, index: slice) -> "Rows[R]": ...

    def __getitem__(self, index: int | slice) -> "R | Rows[R]":
        """Return one record, or a `Rows` of a slice."""
        if isinstance(index, slice):
            return self.__class__.model_construct(root=self.root[index])
        return self.root[index]

    @classmethod
    def _wrap(cls, records: list[R]) -> "Rows[R]":
        """Wrap already-hydrated records without validation.

        Re-validating would put Pydantic back on the hot path that
        direct-to-dict construction avoids (AGENTS.md I-2).
        """
        return cls.model_construct(root=records)
