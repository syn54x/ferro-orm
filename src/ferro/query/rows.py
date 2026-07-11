"""Projected-record containers: ``Row`` and the list-like ``Rows`` (ADR-0007).

A projection (``Transaction.select(lambda t: (t.id, t.amount))``) returns
projected records, never model instances — a model instance always carries a
complete row (the complete-instance invariant). ``Row`` is one record;
``Rows`` is the list-like container a projected query's ``all()`` delivers
them in.

Both are pydantic-shaped (``model_dump()``, FastAPI ``response_model``) but
never pydantic-constructed: instances come off the same direct-to-dict
hydration path as models (AGENTS.md I-2), and ``Rows`` wraps without
re-validating. Pydantic is the interface, not the constructor.
"""

from collections.abc import Iterator
from typing import TypeVar, overload

from pydantic import BaseModel, ConfigDict, RootModel

R = TypeVar("R")


class Row(BaseModel):
    """One projected record: named values from an explicit projection.

    A ``Row`` is not a model instance. It carries no persistence identity —
    no ``save()``, no refresh, no identity-map participation — and its field
    set is whatever the projection selected, in selection order.

    Fields live in pydantic's extra storage (``model_config`` is
    ``extra="allow"``); the Rust hydration path populates them directly
    (I-2), so construction never runs pydantic validation. Attribute access,
    ``model_dump()``, ``model_dump_json()``, equality, and ``repr`` all work
    as on any pydantic model:

    Examples:
        >>> rows = await Transaction.select(lambda t: (t.id, t.amount)).all()  # doctest: +SKIP
        >>> rows[0].amount  # doctest: +SKIP
        Decimal('12.50')
        >>> rows[0].model_dump()  # doctest: +SKIP
        {'id': 1, 'amount': Decimal('12.50')}
    """

    model_config = ConfigDict(extra="allow")


class Rows(RootModel[list[R]]):
    """List-like pydantic container for projected records.

    Delivered by a projected query's ``all()`` as ``Rows[Row]``. Indexing,
    slicing, iteration, ``len()``, and truthiness work like a list; a slice
    returns a ``Rows`` of the same record type. As a pydantic ``RootModel``
    it drops straight into an API response: ``model_dump()`` yields
    ``list[dict]`` and ``Rows[Row]`` works as a FastAPI ``response_model``.

    Examples:
        >>> len(rows), rows[0].id, [r.amount for r in rows]  # doctest: +SKIP
        (3, 1, [Decimal('12.50'), Decimal('7.00'), Decimal('99.99')])
        >>> rows[:2].model_dump()  # doctest: +SKIP
        [{'id': 1, 'amount': Decimal('12.50')}, {'id': 2, 'amount': Decimal('7.00')}]
    """

    def __len__(self) -> int:
        return len(self.root)

    def __iter__(self) -> Iterator[R]:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        # BaseModel.__iter__ yields (field, value) pairs; a Rows iterates its
        # records, like the list it stands in for.
        return iter(self.root)

    @overload
    def __getitem__(self, index: int) -> R: ...

    @overload
    def __getitem__(self, index: slice) -> "Rows[R]": ...

    def __getitem__(self, index: int | slice) -> "R | Rows[R]":
        if isinstance(index, slice):
            return self.__class__.model_construct(root=self.root[index])
        return self.root[index]

    @classmethod
    def _wrap(cls, records: list[R]) -> "Rows[R]":
        """Wrap already-hydrated records WITHOUT validation (ADR-0007).

        The records come off the Rust hydration path; re-validating them here
        would put pydantic back on the hot path that direct-to-dict
        construction exists to avoid (I-2).
        """
        return cls.model_construct(root=records)
