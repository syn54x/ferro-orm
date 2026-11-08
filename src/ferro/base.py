"""Field and relation options that model annotations carry.

[`FerroField`][ferro.FerroField] holds a column's database options,
[`ForeignKey`][ferro.ForeignKey] the owning side of a relation, and
`ForwardRelation` and `ReverseRelation` what
[`Forward`][ferro.Forward] and [`Reverse`][ferro.Reverse] declare.
"""

import warnings
from typing import (
    Any,
    Literal,
    TypeVar,
)

from ._annotation_utils import annotation_allows_none

T = TypeVar("T")
"""A type variable for generic helpers."""

FerroNullable = Literal["infer"] | bool
"""Column nullability: `"infer"` follows the annotation, `True` or `False` forces it.

With `"infer"`, a column is nullable when its annotation allows `None`.
`True` and `False` force `NULL` and `NOT NULL` in every schema Ferro emits,
whatever the annotation says.
"""

DbTypeToken = Literal[
    "text",
    "smallint",
    "int",
    "bigint",
    "uuid",
    "timestamp",
    "timestamptz",
    "date",
    "time",
    "json",
    "jsonb",
]
"""SQL storage type names accepted by `db_type=` on [`Field`][ferro.Field].

See [Column type overrides with `db_type`](../guide/models-and-fields.md#column-type-overrides-with-db_type)
for the types each name maps to on SQLite and Postgres.
"""

DbType = DbTypeToken | str
"""A `db_type=` value: a [`DbTypeToken`][ferro.DbTypeToken] or a `varchar(N)` string.

Build the `varchar(N)` form with [`varchar`][ferro.varchar].
"""


OnDelete = Literal["CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", "NO ACTION"]
"""What the database does to a row when the row its foreign key points at is deleted."""

_ON_DELETE_ACTIONS = ("CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", "NO ACTION")


def varchar(length: int) -> str:
    """Build the `varchar(N)` type name for `db_type=`.

    A call to `varchar` reads as a deliberate type choice, where a raw
    `"varchar(80)"` string could be a typo. Ferro still checks the value
    against the field's annotation when the model class is defined.

    Args:
        length: The maximum length in characters. Must be positive.

    Returns:
        The type name, `"varchar(<length>)"`.

    Raises:
        ValueError: `length` is zero or negative.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import varchar

        assert varchar(80) == "varchar(80)"
        ```
    """
    if length <= 0:
        raise ValueError(f"varchar length must be positive, got {length}")
    return f"varchar({length})"


def _validate_nullable_option(nullable: FerroNullable, owner: str) -> FerroNullable:
    """Validate the public ``nullable`` tri-state option."""
    if nullable == "infer" or isinstance(nullable, bool):
        return nullable
    raise TypeError(f"{owner} nullable must be 'infer', True, or False")


def _validate_renamed_from(renamed_from: str | None, owner: str) -> str | None:
    """Validate a rename hint: ``None`` or a non-empty name (ADR-0032)."""
    if renamed_from is None:
        return None
    if isinstance(renamed_from, str) and renamed_from:
        return renamed_from
    raise TypeError(f"{owner} renamed_from must be a non-empty str or None")


def _validate_retired(retired: bool, owner: str) -> bool:
    """Validate the ``retired`` flag (ADR-0053): a ``bool``."""
    if isinstance(retired, bool):
        return retired
    raise TypeError(f"{owner} retired must be True or False")


def _validate_generated(generated: Any, owner: str) -> Any:
    """Validate the ``generated`` option (ADR-0058): a lambda, or ``None``."""
    if generated is None or callable(generated):
        return generated
    raise TypeError(
        f"{owner} generated must be a lambda over the model's columns (e.g. "
        "generated=lambda transaction: fn.sign(transaction.amount_minor)), not "
        f"{type(generated).__name__}"
    )


def _validate_foreign_key(foreign_key: Any, owner: str) -> Any:
    """Validate ``foreign_key``: a model class, its class name, or ``None``."""
    if foreign_key is None or isinstance(foreign_key, type):
        return foreign_key
    if isinstance(foreign_key, str) and foreign_key:
        return foreign_key
    raise TypeError(
        f"{owner} foreign_key must be a model class or its class name, not "
        f"{type(foreign_key).__name__}"
    )


def _validate_on_delete(on_delete: Any, owner: str) -> Any:
    """Validate ``on_delete``: one of the five referential actions, or ``None``."""
    if on_delete is None or on_delete in _ON_DELETE_ACTIONS:
        return on_delete
    raise TypeError(
        f"{owner} on_delete must be one of "
        f"{', '.join(repr(action) for action in _ON_DELETE_ACTIONS)}, not {on_delete!r}"
    )


class FerroField:
    """Database options for one model field, given inside `Annotated[...]`.

    [`Field`][ferro.Field] takes the same options and is the usual spelling:
    it stores them as one `FerroField` in the field's metadata, never in its
    JSON schema. `FerroField` carries only the database options, for an
    `Annotated` declaration that keeps them apart from Pydantic's own
    `Field`. See
    [Field options](../guide/models-and-fields.md#field-options) for each
    option at work.

    Examples:
        ```python {test=run prelude=reference}
        from typing import Annotated

        from ferro import FerroField, Model, create_tables


        class Tag(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            label: Annotated[str, FerroField(unique=True, index=True)]


        await create_tables()
        tag = await Tag.create(label="history")
        assert tag.id == 1
        ```
    """

    def __init__(
        self,
        primary_key: bool = False,
        autoincrement: bool | None = None,
        unique: bool = False,
        index: bool = False,
        nullable: FerroNullable = "infer",
        db_type: DbType | None = None,
        db_check: bool = False,
        renamed_from: str | None = None,
        retired: bool = False,
        generated: Any = None,
        foreign_key: "type | str | None" = None,
        on_delete: OnDelete | None = None,
    ):
        """Declare the database options for a field.

        Args:
            primary_key: Make the column the table's primary key.
            autoincrement: Whether the database generates the key. Defaults to
                `None`, which lets an integer primary key auto-increment.
            unique: Add a unique constraint on this column alone. For
                uniqueness across several columns, declare
                `__ferro_composite_uniques__` on the model (see
                [Composite uniques](../guide/models-and-fields.md#composite-uniques)).
            index: Add an index on the column.
            nullable: `"infer"` (the default) makes the column nullable when
                the annotation allows `None`; `True` or `False` forces `NULL`
                or `NOT NULL` whatever the annotation says.
            db_type: The column's SQL type, overriding the one Ferro derives
                from the annotation: a [`DbTypeToken`][ferro.DbTypeToken] or
                a [`varchar(N)`][ferro.varchar] name. Defaults to `None`, the
                derived type. A type the annotation cannot be stored as is
                refused when the class is defined.
            db_check: Add a `CHECK` constraint that limits the column to an
                enum's values. Needs `db_type`, since the default native enum
                type already enforces them. Defaults to `False`.
            renamed_from: The column's previous name, so that
                `ferro migrate new` renames the column instead of dropping
                and adding it. See [Rename hints](../guide/schema/renames-and-retirements.md#rename-hints).
            retired: Take the column out of the model while it stays in the
                schema, nullable, until a later release deletes the
                declaration. The annotation must allow `None`. Defaults to
                `False`. See
                [Retiring a column](../guide/schema/renames-and-retirements.md#retiring-a-column).
            generated: A lambda over the model's own columns from which the
                database computes the column on every write. Defaults to
                `None`, a plain column. Give the field a `None` default
                beside it, as [`Field`][ferro.Field] does for you.
            foreign_key: The model whose primary key the column holds: the
                class, or its class name for a self reference or a model
                defined later. Defaults to `None`, a plain column.
            on_delete: What the database does to this row when the row the
                key points at is deleted: `"CASCADE"`, `"RESTRICT"`,
                `"SET NULL"`, `"SET DEFAULT"` or `"NO ACTION"`. Defaults to
                `None`, which is `"CASCADE"` on a foreign-key column.

        Raises:
            TypeError: `nullable`, `renamed_from`, `retired`, `generated`,
                `foreign_key` or `on_delete` has a value of the wrong type.
        """
        self.primary_key = primary_key
        """Whether the column is the table's primary key."""
        self.autoincrement = autoincrement
        """Whether the database generates the key; `None` lets Ferro decide."""
        self.unique = unique
        """Whether the column has a single-column unique constraint."""
        self.index = index
        """Whether the column has an index."""
        self.nullable = _validate_nullable_option(nullable, "FerroField")
        """`"infer"`, or a forced `True` or `False`."""
        self.db_type = db_type
        """The SQL type override, or `None` for the derived type."""
        self.db_check = db_check
        """Whether the column has a `CHECK` constraint on an enum's values."""
        self.renamed_from = _validate_renamed_from(renamed_from, "FerroField")
        """The column's previous name, or `None`."""
        self.retired = _validate_retired(retired, "FerroField")
        """Whether the column is retired from the model."""
        self.generated = _validate_generated(generated, "FerroField")
        """The lambda the database computes the column from, or `None`."""
        self.foreign_key = _validate_foreign_key(foreign_key, "FerroField")
        """The model the column's key points at, its class name, or `None`."""
        on_delete = _validate_on_delete(on_delete, "FerroField")
        self.on_delete: str | None = (
            "CASCADE" if on_delete is None and foreign_key is not None else on_delete
        )
        """The referential action on a foreign-key column, or `None` when unset.

        A foreign-key column declared without one holds `"CASCADE"`.
        """


class ForeignKey:
    """The owning side of a relation: a foreign key to another model.

    Declare it as `Annotated[Target, ForeignKey(...)]`. The table gets a
    `<field>_id` column holding the target's primary key, and the target
    model reads the relation back through the [`BackRef`][ferro.BackRef]
    that `related_name` names. The models these examples run against declare
    the pair like this, other fields left out:

    ```python {test=compile reason="the reference prelude already declares these models"}
    class Author(Model):
        ...
        posts: Relation[list["Post"]] = BackRef()


    class Post(Model):
        ...
        author: Annotated[Author, ForeignKey(related_name="posts")]
    ```

    See [Relationships](../guide/relationships.md) for one-to-one, nullable
    and self-referential relations.

    Examples:
        ```python {test=run prelude=reference}
        post = await Post.where(lambda post: post.views > 1000).first()
        author = await post.author
        assert author.name == "Ada Lovelace"
        ```
    """

    def __init__(
        self,
        related_name: str | None = None,
        on_delete: str = "CASCADE",
        unique: bool = False,
        index: bool = False,
        nullable: FerroNullable = "infer",
        renamed_from: str | None = None,
        retired: bool = False,
    ):
        """Declare a foreign key to the model in the annotation.

        Args:
            related_name: The name of the [`BackRef`][ferro.BackRef] on the
                target model that reads this relation back. Required unless
                `retired=True`.
            on_delete: What the database does to this row when the target row
                is deleted: `"CASCADE"` (the default), `"RESTRICT"`,
                `"SET NULL"`, `"SET DEFAULT"` or `"NO ACTION"`.
            unique: Make the relation one-to-one with a unique constraint on
                the `<field>_id` column. The constraint indexes the column, so
                `index=True` beside it is ignored with a `UserWarning`.
            index: Add a non-unique index on the `<field>_id` column. Postgres
                does not index a foreign key column on its own.
            nullable: `"infer"` (the default) makes the `<field>_id` column
                nullable when the relation's annotation allows `None` or
                `on_delete` is `"SET NULL"`; `True` or `False` forces it.
            renamed_from: The relation's previous field name. The
                `<field>_id` column and its foreign key constraint are renamed
                with it (`writer` to `author` renames `writer_id` to
                `author_id`). See [Rename hints](../guide/schema/renames-and-retirements.md#rename-hints).
            retired: Take the relation and its `<field>_id` column out of the
                model while the column and its foreign key stay in the schema,
                nullable, until a later release deletes the declaration.
                Defaults to `False`. See
                [Retiring a column](../guide/schema/renames-and-retirements.md#retiring-a-column).

        Raises:
            TypeError: `related_name` is missing on a relation that is not
                retired, or an option has a value of the wrong type.
            ValueError: `on_delete="SET NULL"` with `nullable=False`; the
                database could not set the column to `NULL`.
        """
        self.to = None  # Resolved later
        """The target model class, set once Ferro resolves the relation."""
        self.related_name = related_name
        """The name of the reverse relation on the target model."""
        self.on_delete = on_delete
        """The referential action when the target row is deleted."""
        self.unique = unique
        """Whether the relation is one-to-one."""
        self.index = index
        """Whether the `<field>_id` column has a non-unique index."""
        self.nullable = _validate_nullable_option(nullable, "ForeignKey")
        """`"infer"`, or a forced `True` or `False`."""
        self.renamed_from = _validate_renamed_from(renamed_from, "ForeignKey")
        """The relation's previous field name, or `None`."""
        self.retired = _validate_retired(retired, "ForeignKey")
        """Whether the relation is retired from the model."""
        if related_name is None and not self.retired:
            raise TypeError(
                "ForeignKey requires related_name, the BackRef on the target "
                'model that reads this relation back (ForeignKey(related_name="posts")); '
                "only a retired relation (retired=True) has none"
            )
        if str(self.on_delete).upper() == "SET NULL" and self.nullable is False:
            raise ValueError(
                "ForeignKey(on_delete='SET NULL') requires nullable=True or 'infer'"
            )
        if unique and index:
            warnings.warn(
                "ForeignKey(unique=True, index=True) is redundant; unique=True "
                "already implies an index. Ignoring index=True.",
                UserWarning,
                stacklevel=2,
            )
            self.index = False
        # Set by the metaclass: forward fields are not in ``model_fields``, so
        # nullability inference reads the relation's annotation from here.
        self.relation_annotation: Any | None = None
        """The relation's annotation, set when the model class is defined."""


def foreign_key_allows_none(metadata: "ForeignKey") -> bool | None:
    """Decide whether a foreign key's `<field>_id` column is nullable.

    Args:
        metadata: The relation's foreign key options.

    Returns:
        `True` or `False` when `nullable` forces it or `on_delete` is
        `"SET NULL"`; otherwise whether the relation's annotation allows
        `None`, or `None` when the annotation is not known yet.
    """
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


class ForwardRelation:
    """A forward relation over a foreign-key column of its own model.

    [`Forward`][ferro.Forward] declares it; the class keeps it in
    `__ferro_relations__`.
    """

    def __init__(
        self,
        fk_column: str,
        to: Any,
        nullable: bool,
        refusal: str | None = None,
    ) -> None:
        """Store what the declaration says.

        Args:
            fk_column: The foreign-key column the relation reads.
            to: The model in the relation's annotation, or its name until
                Ferro resolves it; `None` when the annotation names none.
            nullable: Whether the annotation allows `None`.
            refusal: Why the declaration is refused, when the class body
                cannot name the target its fix writes; relation resolution
                raises it with the target named. Defaults to `None`.
        """
        self.fk_column = fk_column
        """The foreign-key column the relation reads."""
        self.to = to
        """The target model class, or its name until Ferro resolves it."""
        self.nullable = nullable
        """Whether the relation's annotation allows `None`."""
        self.refusal = refusal
        """A refusal waiting for resolution to name its target, or `None`."""


class ReverseRelation:
    """A reverse relation over a foreign-key column of another model.

    [`Reverse`][ferro.Reverse] declares it; the class keeps it in
    `__ferro_relations__`.
    """

    def __init__(self, attribute: str, to: Any, refusal: str | None = None) -> None:
        """Store what the declaration says.

        Args:
            attribute: The foreign-key column on the model `to`.
            to: The model in the relation's annotation, or its name until
                Ferro resolves it; `None` when the annotation names none.
            refusal: Why the declaration is refused, when the class body
                cannot name the model its fix writes; relation resolution
                raises it with the model named. Defaults to `None`.
        """
        self.attribute = attribute
        """The foreign-key column on the other model that points back."""
        self.to = to
        """The other model's class, or its name until Ferro resolves it."""
        self.refusal = refusal
        """A refusal waiting for resolution to name its model, or `None`."""


class ManyToManyRelation:
    """The options of a many-to-many relation, as Ferro stores them on the model.

    Declare the relation with [`ManyToMany`][ferro.ManyToMany], which builds
    this object; see
    [Many-to-many](../guide/relationships.md#many-to-many).
    """

    def __init__(
        self,
        related_name: str,
        through: str | None = None,
        reverse_index: bool = True,
    ):
        """Store the options of a many-to-many relation.

        Args:
            related_name: The name of the relation on the target model that
                reads this one back.
            through: The join table's name. Defaults to `None`, a name Ferro
                derives from the two models.
            reverse_index: Add an index on the join table's
                `(target, source)` columns for queries from the other side.
                Defaults to `True`; turn it off for a write-heavy join table.
        """
        self.to = None  # Resolved later
        """The target model class, set once Ferro resolves the relation."""
        self.related_name = related_name
        """The name of the reverse relation on the target model."""
        self.through = through
        """The join table's name, or `None` for the derived one."""
        self.reverse_index = reverse_index
        """Whether the join table has an index for reverse queries."""
