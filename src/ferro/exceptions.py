"""ORM-specific exceptions."""

from typing import Any


class ModelDoesNotExist(LookupError):
    """Raised when :meth:`~ferro.models.Model.get` finds no row for the primary key."""

    model: type
    pk: Any

    def __init__(self, model_cls: type, pk: Any) -> None:
        self.model = model_cls
        self.pk = pk
        super().__init__(
            f"No {model_cls.__name__} record found for primary key {pk!r}"
        )
