from typing import (
    TypeVar,
)

T = TypeVar("T")


class FerroField:
    """
    Metadata container for Ferro-specific field configuration.

    This class is used to define database-level constraints like primary keys,
    uniqueness, and indexes. It is typically used within `typing.Annotated`.
    """

    def __init__(
        self,
        primary_key: bool = False,
        autoincrement: bool | None = None,
        unique: bool = False,
        index: bool = False,
    ):
        """
        Initialize Ferro field metadata.

        Args:
            primary_key: Whether this field is the primary key.
            autoincrement: Whether the database should automatically increment this value.
                Defaults to True for integer primary keys.
            unique: Whether to enforce a uniqueness constraint.
            index: Whether to create a database index for this column.
        """
        self.primary_key = primary_key
        self.autoincrement = autoincrement
        self.unique = unique
        self.index = index


class ForeignKey:
    """
    Metadata for a forward relationship (One-to-Many or One-to-One).
    """

    def __init__(
        self, related_name: str, on_delete: str = "CASCADE", unique: bool = False
    ):
        """
        Initialize a Foreign Key relationship.

        Args:
            related_name: The name of the field to be added to the target model for reverse lookup.
            on_delete: The referential action to take when the parent record is deleted.
                Options: "CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", "NO ACTION".
            unique: If True, this relationship is treated as a strict One-to-One link.
        """
        self.to = None  # Resolved later
        self.related_name = related_name
        self.on_delete = on_delete
        self.unique = unique


class ManyToManyField:
    """
    Metadata for a Many-to-Many relationship.
    """

    def __init__(self, related_name: str, through: str | None = None):
        """
        Initialize a Many-to-Many relationship.

        Args:
            related_name: The name of the field to be added to the target model for reverse lookup.
            through: The name of the join table. If None, Ferro automatically generates one.
        """
        self.to = None  # Resolved later
        self.related_name = related_name
        self.through = through
