# Field Metadata

## `FerroField`

Store database column metadata for a model field

Attributes:

```text
primary_key: Mark the field as the table primary key.
autoincrement: Override automatic increment behavior for primary key columns.
unique: Enforce a uniqueness constraint for the column.
index: Request an index for the column.
```

Examples:

```pycon
>>> from typing import Annotated
>>> from ferro.models import Model
>>>
>>> class User(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
...     email: Annotated[str, FerroField(unique=True, index=True)]
```

Source code in `src/ferro/base.py`

```python
class FerroField:
    """Store database column metadata for a model field

    Attributes:

        primary_key: Mark the field as the table primary key.
        autoincrement: Override automatic increment behavior for primary key columns.
        unique: Enforce a uniqueness constraint for the column.
        index: Request an index for the column.

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
    ):
        """Initialize field metadata options

        Args:

            primary_key: Set to True when the field is the primary key.
            autoincrement: Control whether the database auto-increments the value.
                When not provided, the backend infers a default for integer primary keys.
            unique: Set to True to enforce uniqueness.
            index: Set to True to create a database index.

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
```

### Attributes

#### `primary_key = primary_key`

#### `autoincrement = autoincrement`

#### `unique = unique`

#### `index = index`

### Functions

#### `__init__(primary_key=False, autoincrement=None, unique=False, index=False)`

Initialize field metadata options

Args:

```text
primary_key: Set to True when the field is the primary key.
autoincrement: Control whether the database auto-increments the value.
    When not provided, the backend infers a default for integer primary keys.
unique: Set to True to enforce uniqueness.
index: Set to True to create a database index.
```

Examples:

```pycon
>>> from typing import Annotated
>>> from ferro.models import Model
>>>
>>> class User(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
...     created_at: Annotated[int, FerroField(index=True)]
```

Source code in `src/ferro/base.py`

```python
def __init__(
    self,
    primary_key: bool = False,
    autoincrement: bool | None = None,
    unique: bool = False,
    index: bool = False,
):
    """Initialize field metadata options

    Args:

        primary_key: Set to True when the field is the primary key.
        autoincrement: Control whether the database auto-increments the value.
            When not provided, the backend infers a default for integer primary keys.
        unique: Set to True to enforce uniqueness.
        index: Set to True to create a database index.

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
```

## `ForeignKey`

Describe a forward foreign-key relationship between models

Attributes:

```text
to: Target model class resolved during model binding.
related_name: Name of the reverse relationship attribute on the target model.
on_delete: Referential action applied when the parent row is deleted.
unique: Treat the relation as one-to-one when True.
```

Examples:

```pycon
>>> from typing import Annotated
>>> from ferro.models import Model
>>>
>>> class User(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
>>>
>>> class Post(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
...     author: Annotated[int, ForeignKey("posts", on_delete="CASCADE")]
```

Source code in `src/ferro/base.py`

```python
class ForeignKey:
    """Describe a forward foreign-key relationship between models

    Attributes:

        to: Target model class resolved during model binding.
        related_name: Name of the reverse relationship attribute on the target model.
        on_delete: Referential action applied when the parent row is deleted.
        unique: Treat the relation as one-to-one when True.

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
        self, related_name: str, on_delete: str = "CASCADE", unique: bool = False
    ):
        """Initialize foreign-key relationship metadata

        Args:

            related_name: Name for reverse access from the related model.
            on_delete: Referential action for parent deletion.
                Common values include "CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", and "NO ACTION".
            unique: Set to True to enforce one-to-one behavior.

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
```

### Attributes

#### `to = None`

#### `related_name = related_name`

#### `on_delete = on_delete`

#### `unique = unique`

### Functions

#### `__init__(related_name, on_delete='CASCADE', unique=False)`

Initialize foreign-key relationship metadata

Args:

```text
related_name: Name for reverse access from the related model.
on_delete: Referential action for parent deletion.
    Common values include "CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", and "NO ACTION".
unique: Set to True to enforce one-to-one behavior.
```

Examples:

```pycon
>>> from typing import Annotated
>>> from ferro.models import Model
>>>
>>> class User(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
...     profile_id: Annotated[int, ForeignKey("user", unique=True)]
```

Source code in `src/ferro/base.py`

```python
def __init__(
    self, related_name: str, on_delete: str = "CASCADE", unique: bool = False
):
    """Initialize foreign-key relationship metadata

    Args:

        related_name: Name for reverse access from the related model.
        on_delete: Referential action for parent deletion.
            Common values include "CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", and "NO ACTION".
        unique: Set to True to enforce one-to-one behavior.

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
```

## `ManyToManyField`

Describe metadata for a many-to-many relationship

Attributes:

```text
to: Target model class resolved during model binding.
related_name: Name of the reverse relationship attribute on the target model.
through: Optional join table name used for the association.
```

Examples:

```pycon
>>> from typing import Annotated
>>> from ferro.models import Model
>>>
>>> class Tag(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
>>>
>>> class Post(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
...     tags: Annotated[list[int], ManyToManyField("posts")]
```

Source code in `src/ferro/base.py`

```python
class ManyToManyField:
    """Describe metadata for a many-to-many relationship

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
        ...     tags: Annotated[list[int], ManyToManyField("posts")]
    """

    def __init__(self, related_name: str, through: str | None = None):
        """Initialize many-to-many relationship metadata

        Args:

            related_name: Name for reverse access from the related model.
            through: Explicit join table name.
                When omitted, Ferro generates a join table name automatically.

        Examples:
            >>> from typing import Annotated
            >>> from ferro.models import Model
            >>>
            >>> class User(Model):
            ...     id: Annotated[int, FerroField(primary_key=True)]
            ...     teams: Annotated[list[int], ManyToManyField("members", through="team_members")]
        """
        self.to = None  # Resolved later
        self.related_name = related_name
        self.through = through
```

### Attributes

#### `to = None`

#### `related_name = related_name`

#### `through = through`

### Functions

#### `__init__(related_name, through=None)`

Initialize many-to-many relationship metadata

Args:

```text
related_name: Name for reverse access from the related model.
through: Explicit join table name.
    When omitted, Ferro generates a join table name automatically.
```

Examples:

```pycon
>>> from typing import Annotated
>>> from ferro.models import Model
>>>
>>> class User(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
...     teams: Annotated[list[int], ManyToManyField("members", through="team_members")]
```

Source code in `src/ferro/base.py`

```python
def __init__(self, related_name: str, through: str | None = None):
    """Initialize many-to-many relationship metadata

    Args:

        related_name: Name for reverse access from the related model.
        through: Explicit join table name.
            When omitted, Ferro generates a join table name automatically.

    Examples:
        >>> from typing import Annotated
        >>> from ferro.models import Model
        >>>
        >>> class User(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        ...     teams: Annotated[list[int], ManyToManyField("members", through="team_members")]
    """
    self.to = None  # Resolved later
    self.related_name = related_name
    self.through = through
```
