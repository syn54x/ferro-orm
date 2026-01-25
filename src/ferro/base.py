from typing import (
    Generic,
    TypeVar,
)

T = TypeVar("T")


class FerroField:
    """
    Metadata container for Ferro-specific field configuration.
    """

    def __init__(
        self,
        primary_key: bool = False,
        autoincrement: bool | None = None,
        unique: bool = False,
        index: bool = False,
    ):
        self.primary_key = primary_key
        self.autoincrement = autoincrement
        self.unique = unique
        self.index = index


class ForeignKey:
    """Metadata for a forward relationship."""

    def __init__(
        self, related_name: str, on_delete: str = "CASCADE", unique: bool = False
    ):
        self.to = None  # Resolved later
        self.related_name = related_name
        self.on_delete = on_delete
        self.unique = unique


class ManyToManyField:
    """Metadata for a many-to-many relationship."""

    def __init__(self, related_name: str, through: str | None = None):
        self.to = None  # Resolved later
        self.related_name = related_name
        self.through = through
