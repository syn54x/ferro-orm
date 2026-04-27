"""Define field and relationship metadata primitives for Ferro models.

This module provides lightweight configuration objects used by model annotations to describe column constraints and inter-model relationships.
"""

from typing import (
    Any,
    Literal,
    TypeVar,
)

from ._annotation_utils import annotation_allows_none

T = TypeVar("T")

FerroNullable = Literal["infer"] | bool
"""Alembic column nullability: ``'infer'`` from the field type, or forced bool."""


def _validate_nullable_option(nullable: FerroNullable, owner: str) -> FerroNullable:
    """Validate the public ``nullable`` tri-state option."""
    if nullable == "infer" or isinstance(nullable, bool):
        return nullable
    raise TypeError(f"{owner} nullable must be 'infer', True, or False")


class FerroField:
    """Store database column metadata for a model field

    Attributes:

        primary_key: Mark the field as the table primary key.
        autoincrement: Override automatic increment behavior for primary key columns.
        unique: Enforce a **single-column** uniqueness constraint for this column only.
            For uniqueness on multiple columns together, declare
            ``__ferro_composite_uniques__`` on the :class:`ferro.models.Model` subclass
            (see the models guide).
        index: Request an index for the column.
        nullable: Alembic ``Column.nullable`` when using :func:`~ferro.migrations.get_metadata`.
            ``'infer'`` (default) uses whether the annotation allows ``None``.
            ``False`` / ``True`` force NOT NULL / NULL regardless of the type (for
            advanced cases such as ``int | None`` used only for static typing).

    Examples:
        >>> from typing import Annotated
        >>> from ferro.models import Model
        >>>
        >>> class User(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        ...     email: Annotated[str, FerroField(unique=True, index=True)]
    """

    def __init__(
        self,
        primary_key: bool = False,
        autoincrement: bool | None = None,
        unique: bool = False,
        index: bool = False,
        nullable: FerroNullable = "infer",
    ):
        """Initialize field metadata options

        Args:

            primary_key: Set to True when the field is the primary key.
            autoincrement: Control whether the database auto-increments the value.
                When not provided, the backend infers a default for integer primary keys.
            unique: Set to True to enforce **single-column** uniqueness only.
            index: Set to True to create a database index.
            nullable: See :class:`FerroField` attribute ``nullable`` in the class docstring.

        Examples:
            >>> from typing import Annotated
            >>> from ferro.models import Model
            >>>
            >>> class User(Model):
            ...     id: Annotated[int, FerroField(primary_key=True)]
            ...     created_at: Annotated[int, FerroField(index=True)]
        """
        self.primary_key = primary_key
        self.autoincrement = autoincrement
        self.unique = unique
        self.index = index
        self.nullable = _validate_nullable_option(nullable, "FerroField")


class ForeignKey:
    """Describe a forward foreign-key relationship between models

    Attributes:

        to: Target model class resolved during model binding.
        related_name: Name of the reverse relationship attribute on the target model.
        on_delete: Referential action applied when the parent row is deleted.
        unique: Treat the relation as one-to-one when True.
        nullable: Alembic nullability for the shadow ``*_id`` column (see
            :class:`FerroField` ``nullable``). When ``'infer'``, uses whether the
            **relation** annotation allows ``None``.

    Examples:
        >>> from typing import Annotated
        >>> from ferro.models import Model
        >>>
        >>> class User(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        >>>
        >>> class Post(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        ...     author: Annotated[int, ForeignKey("posts", on_delete="CASCADE")]
    """

    def __init__(
        self,
        related_name: str,
        on_delete: str = "CASCADE",
        unique: bool = False,
        nullable: FerroNullable = "infer",
    ):
        """Initialize foreign-key relationship metadata

        Args:

            related_name: Name for reverse access from the related model.
            on_delete: Referential action for parent deletion.
                Common values include "CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", and "NO ACTION".
            unique: Set to True to enforce one-to-one behavior.
            nullable: See :class:`ForeignKey` class docstring.

        Examples:
            >>> from typing import Annotated
            >>> from ferro.models import Model
            >>>
            >>> class User(Model):
            ...     id: Annotated[int, FerroField(primary_key=True)]
            ...     profile_id: Annotated[int, ForeignKey("user", unique=True)]
        """
        self.to = None  # Resolved later
        self.related_name = related_name
        self.on_delete = on_delete
        self.unique = unique
        self.nullable = _validate_nullable_option(nullable, "ForeignKey")
        if str(self.on_delete).upper() == "SET NULL" and self.nullable is False:
            raise ValueError(
                "ForeignKey(on_delete='SET NULL') requires nullable=True or 'infer'"
            )
        #: First type argument of ``Annotated[..., ForeignKey]``; set by the metaclass
        #: for Alembic nullability inference (forward fields are not in ``model_fields``).
        self.relation_annotation: Any | None = None


def foreign_key_allows_none(metadata: "ForeignKey") -> bool | None:
    """Effective FK nullability from explicit override, delete action, and relation type."""
    if metadata.nullable is True:
        return True
    if metadata.nullable is False:
        return False
    if str(metadata.on_delete).upper() == "SET NULL":
        return True
    relation_annotation = getattr(metadata, "relation_annotation", None)
    if relation_annotation is None:
        return None
    return annotation_allows_none(relation_annotation)


class ManyToManyRelation:
    """Describe internal metadata for a many-to-many relationship

    Attributes:

        to: Target model class resolved during model binding.
        related_name: Name of the reverse relationship attribute on the target model.
        through: Optional join table name used for the association.

    Examples:
        >>> from typing import Annotated
        >>> from ferro.models import Model
        >>>
        >>> class Tag(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        >>>
        >>> class Post(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        ...     tags: Relation[list["Tag"]] = ManyToMany(related_name="posts")
    """

    def __init__(
        self,
        related_name: str,
        through: str | None = None,
        reverse_index: bool = True,
    ):
        """Initialize many-to-many relationship metadata

        Args:

            related_name: Name for reverse access from the related model.
            through: Explicit join table name.
                When omitted, Ferro generates a join table name automatically.
            reverse_index: When True (default), the synthesized join table
                gets a non-unique composite index on ``(target_col, source_col)``
                to optimize back-ref queries. Set to False to opt out (e.g.,
                write-heavy join tables where the extra index cost is unwanted).

        Examples:
            >>> from typing import Annotated
            >>> from ferro.models import Model
            >>>
            >>> class User(Model):
            ...     id: Annotated[int, FerroField(primary_key=True)]
            ...     teams: Relation[list["Team"]] = ManyToMany(related_name="members", through="team_members")
        """
        self.to = None  # Resolved later
        self.related_name = related_name
        self.through = through
        self.reverse_index = reverse_index
