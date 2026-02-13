# Core Models

## `Model`

Bases: `BaseModel`

Provide the base class for all Ferro models

Inheriting from this class registers schema metadata with the Rust core and exposes high-performance CRUD and query entrypoints.

Examples:

```pycon
>>> class User(Model):
...     id: int | None = None
...     name: str
```

### Attributes

#### `model_config = ConfigDict(from_attributes=True, use_attribute_docstrings=True, arbitrary_types_allowed=True)`

### Functions

#### `__init__(**data)`

Initialize a model instance and normalize relationship inputs

Parameters:

| Name     | Type  | Description                               | Default |
| -------- | ----- | ----------------------------------------- | ------- |
| `**data` | `Any` | Field values used to construct the model. | `{}`    |

Examples:

```pycon
>>> user = User(name="Taylor")
>>> isinstance(user, User)
True
```

#### `save()`

Persist the current model instance

Returns:

| Type   | Description |
| ------ | ----------- |
| `None` | None        |

Examples:

```pycon
>>> user = User(name="Taylor")
>>> await user.save()
```

#### `delete()`

Delete the current model instance from storage

Returns:

| Type   | Description |
| ------ | ----------- |
| `None` | None        |

Examples:

```pycon
>>> user = await User.get(1)
>>> if user:
...     await user.delete()
```

#### `all()`

Fetch all records for this model class

Returns:

| Type         | Description                         |
| ------------ | ----------------------------------- |
| `list[Self]` | A list of hydrated model instances. |

Examples:

```pycon
>>> users = await User.all()
>>> isinstance(users, list)
True
```

#### `get(pk)`

Fetch one record by primary key value

Parameters:

| Name | Type  | Description                                 | Default    |
| ---- | ----- | ------------------------------------------- | ---------- |
| `pk` | `Any` | Primary key value to fetch a single record. | *required* |

Returns:

| Type   | Description |
| ------ | ----------- |
| \`Self | None\`      |

Examples:

```pycon
>>> user = await User.get(1)
>>> user is None or isinstance(user, User)
True
```

#### `refresh()`

Reload this instance from storage using its primary key

Returns:

| Type   | Description |
| ------ | ----------- |
| `None` | None        |

Raises:

| Type           | Description                                                    |
| -------------- | -------------------------------------------------------------- |
| `RuntimeError` | If no primary key is available or the record no longer exists. |

Examples:

```pycon
>>> user = await User.get(1)
>>> if user:
...     await user.refresh()
```

#### `where(node)`

Start a fluent query with an initial condition

Parameters:

| Name   | Type        | Description                          | Default    |
| ------ | ----------- | ------------------------------------ | ---------- |
| `node` | `QueryNode` | Query predicate node to apply first. | *required* |

Returns:

| Type          | Description                                |
| ------------- | ------------------------------------------ |
| `Query[Self]` | A query object scoped to this model class. |

Examples:

```pycon
>>> query = User.where(User.id == 1)
>>> isinstance(query, Query)
True
```

#### `select()`

Start an empty fluent query for this model class

Returns:

| Type          | Description                                |
| ------------- | ------------------------------------------ |
| `Query[Self]` | A query object scoped to this model class. |

Examples:

```pycon
>>> query = User.select().limit(5)
>>> isinstance(query, Query)
True
```

#### `create(**fields)`

Create and persist a new model instance

Parameters:

| Name       | Type | Description                          | Default |
| ---------- | ---- | ------------------------------------ | ------- |
| `**fields` |      | Field values to construct the model. | `{}`    |

Returns:

| Type   | Description                                     |
| ------ | ----------------------------------------------- |
| `Self` | The newly created and persisted model instance. |

Examples:

```pycon
>>> user = await User.create(name="Taylor")
>>> isinstance(user, User)
True
```

#### `bulk_create(instances)`

Persist multiple instances in a single bulk operation

Parameters:

| Name        | Type         | Description                 | Default    |
| ----------- | ------------ | --------------------------- | ---------- |
| `instances` | `list[Self]` | Model instances to persist. | *required* |

Returns:

| Type  | Description                     |
| ----- | ------------------------------- |
| `int` | The number of records inserted. |

Examples:

```pycon
>>> rows = await User.bulk_create([User(name="A"), User(name="B")])
>>> isinstance(rows, int)
True
```

#### `get_or_create(defaults=None, **fields)`

Fetch a record by filters or create one when missing

Parameters:

| Name       | Type             | Description                          | Default                                         |
| ---------- | ---------------- | ------------------------------------ | ----------------------------------------------- |
| `defaults` | \`dict[str, Any] | None\`                               | Values applied only when creating a new record. |
| `**fields` |                  | Exact-match filters used for lookup. | `{}`                                            |

Returns:

| Type                | Description                                                           |
| ------------------- | --------------------------------------------------------------------- |
| `tuple[Self, bool]` | A tuple of (instance, created) where created is True for new records. |

Examples:

```pycon
>>> user, created = await User.get_or_create(email="a@b.com")
>>> isinstance(created, bool)
True
```

#### `update_or_create(defaults=None, **fields)`

Update a matched record or create one when missing

Parameters:

| Name       | Type             | Description                          | Default                                   |
| ---------- | ---------------- | ------------------------------------ | ----------------------------------------- |
| `defaults` | \`dict[str, Any] | None\`                               | Values applied on update or create paths. |
| `**fields` |                  | Exact-match filters used for lookup. | `{}`                                      |

Returns:

| Type                | Description                                                           |
| ------------------- | --------------------------------------------------------------------- |
| `tuple[Self, bool]` | A tuple of (instance, created) where created is True for new records. |

Examples:

```pycon
>>> user, created = await User.update_or_create(
...     email="a@b.com",
...     defaults={"name": "Taylor"},
... )
>>> isinstance(created, bool)
True
```
