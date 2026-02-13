# Query Builder

## `Query`

Bases: `Generic[T]`

Build and execute fluent ORM queries.

Attributes:

| Name              | Type                   | Description                             |
| ----------------- | ---------------------- | --------------------------------------- |
| `model_cls`       |                        | Model class used to hydrate results.    |
| `where_clause`    | `list[QueryNode]`      | Accumulated filter nodes for the query. |
| `order_by_clause` | `list[dict[str, str]]` | Sort definitions sent to the Rust core. |

### Attributes

#### `model_cls = model_cls`

#### `where_clause = []`

#### `order_by_clause = []`

### Functions

#### `__init__(model_cls)`

Initialize a query for a model class.

Parameters:

| Name        | Type      | Description                                | Default    |
| ----------- | --------- | ------------------------------------------ | ---------- |
| `model_cls` | `Type[T]` | Model class that defines the target table. | *required* |

Examples:

```pycon
>>> query = Query(User)
>>> query.model_cls is User
True
```

#### `where(node)`

Add a filter condition to the query

Parameters:

| Name   | Type        | Description                                                  | Default    |
| ------ | ----------- | ------------------------------------------------------------ | ---------- |
| `node` | `QueryNode` | A QueryNode representing the condition (e.g., User.id == 1). | *required* |

Returns:

| Type       | Description                              |
| ---------- | ---------------------------------------- |
| `Query[T]` | The current Query instance for chaining. |

Examples:

```pycon
>>> query = User.where(User.id == 1)
>>> isinstance(query, Query)
True
```

#### `order_by(field, direction='asc')`

Add an ordering clause to the query

Parameters:

| Name        | Type  | Description                                  | Default    |
| ----------- | ----- | -------------------------------------------- | ---------- |
| `field`     | `Any` | The field to order by (e.g., User.username). | *required* |
| `direction` | `str` | The direction of the sort ("asc" or "desc"). | `'asc'`    |

Returns:

| Type       | Description                              |
| ---------- | ---------------------------------------- |
| `Query[T]` | The current Query instance for chaining. |

Raises:

| Type         | Description                          |
| ------------ | ------------------------------------ |
| `ValueError` | If direction is not "asc" or "desc". |

Examples:

```pycon
>>> query = User.select().order_by(User.username, "desc")
>>> query.order_by_clause[-1]["direction"]
'desc'
```

#### `limit(value)`

Limit the number of records returned

Parameters:

| Name    | Type  | Description                              | Default    |
| ------- | ----- | ---------------------------------------- | ---------- |
| `value` | `int` | The maximum number of records to return. | *required* |

Returns:

| Type       | Description                              |
| ---------- | ---------------------------------------- |
| `Query[T]` | The current Query instance for chaining. |

Examples:

```pycon
>>> query = User.select().limit(10)
>>> query._limit
10
```

#### `offset(value)`

Skip a specific number of records

Parameters:

| Name    | Type  | Description                    | Default    |
| ------- | ----- | ------------------------------ | ---------- |
| `value` | `int` | The number of records to skip. | *required* |

Returns:

| Type       | Description                              |
| ---------- | ---------------------------------------- |
| `Query[T]` | The current Query instance for chaining. |

Examples:

```pycon
>>> query = User.select().offset(20)
>>> query._offset
20
```

#### `all()`

Return all model instances that match the current query

Returns:

| Type      | Description                |
| --------- | -------------------------- |
| `list[T]` | A list of model instances. |

Examples:

```pycon
>>> users = await User.where(User.active == True).all()
>>> isinstance(users, list)
True
```

#### `count()`

Return the number of records that match the current query

Returns:

| Type  | Description                    |
| ----- | ------------------------------ |
| `int` | The count of matching records. |

Examples:

```pycon
>>> total = await User.where(User.active == True).count()
>>> isinstance(total, int)
True
```

#### `update(**fields)`

Update all records matching the current query

Parameters:

| Name       | Type | Description                       | Default |
| ---------- | ---- | --------------------------------- | ------- |
| `**fields` |      | Field names and values to update. | `{}`    |

Returns:

| Type  | Description                    |
| ----- | ------------------------------ |
| `int` | The number of records updated. |

Examples:

```pycon
>>> updated = await User.where(User.id == 1).update(name="Taylor")
>>> isinstance(updated, int)
True
```

#### `first()`

Return the first matching record, or None

Returns:

| Type | Description |
| ---- | ----------- |
| \`T  | None\`      |

Examples:

```pycon
>>> user = await User.select().order_by(User.id).first()
>>> user is None or isinstance(user, User)
True
```

#### `delete()`

Delete all records matching the current query

Returns:

| Type  | Description                    |
| ----- | ------------------------------ |
| `int` | The number of records deleted. |

Examples:

```pycon
>>> deleted = await User.where(User.disabled == True).delete()
>>> isinstance(deleted, int)
True
```

#### `exists()`

Return whether at least one record matches the current query

Returns:

| Type   | Description                             |
| ------ | --------------------------------------- |
| `bool` | True if records exist, otherwise False. |

Examples:

```pycon
>>> found = await User.where(User.email == "a@b.com").exists()
>>> isinstance(found, bool)
True
```

#### `add(*instances)`

Add links to a many-to-many relationship

Parameters:

| Name         | Type  | Description                                          | Default |
| ------------ | ----- | ---------------------------------------------------- | ------- |
| `*instances` | `Any` | Target model instances that provide an id attribute. | `()`    |

Raises:

| Type           | Description                                          |
| -------------- | ---------------------------------------------------- |
| `RuntimeError` | If the query is not bound to a many-to-many context. |

Examples:

```pycon
>>> user = await User.create(email="taylor@example.com")
>>> admin = await Group.create(name="admin")
>>> staff = await Group.create(name="staff")
>>> await user.groups.add(admin, staff)
```

#### `remove(*instances)`

Remove links from a many-to-many relationship

Parameters:

| Name         | Type  | Description                                          | Default |
| ------------ | ----- | ---------------------------------------------------- | ------- |
| `*instances` | `Any` | Target model instances that provide an id attribute. | `()`    |

Raises:

| Type           | Description                                          |
| -------------- | ---------------------------------------------------- |
| `RuntimeError` | If the query is not bound to a many-to-many context. |

Examples:

```pycon
>>> user = await User.create(email="taylor@example.com")
>>> admin = await Group.create(name="admin")
>>> await user.groups.remove(admin)
```

#### `clear()`

Clear all links in a many-to-many relationship

Raises:

| Type           | Description                                          |
| -------------- | ---------------------------------------------------- |
| `RuntimeError` | If the query is not bound to a many-to-many context. |

Examples:

```pycon
>>> user = await User.create(email="taylor@example.com")
>>> await user.groups.clear()
```

#### `__repr__()`

Return a developer-friendly representation of the query

## `BackRelationship`

Bases: `Query[T]`

Represent reverse relationship queries with Query typing support

Examples:

```pycon
>>> class User(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
...     name: str
...     posts: BackRelationship[list["Post"]] = None
```

```pycon
>>> class Post(Model):
...     id: Annotated[int, FerroField(primary_key=True)]
...     title: str
...     user: Annotated[User, ForeignKey(related_name="posts")]
```

```pycon
>>> user = await User.get(1)
>>> posts = await user.posts.all()
>>> isinstance(posts, list)
True
```

### Functions

#### `__get_pydantic_core_schema__(_source_type, _handler)`

Allow pydantic-core to treat relationships as arbitrary runtime values
