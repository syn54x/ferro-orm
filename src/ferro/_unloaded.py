"""The unloaded state of a model instance (ADR-0063), owned in one place.

A forward relation nothing loaded reads as an unloaded instance: a real
instance of its model that holds only its primary key. The Rust hydrator
builds and marks it; every Python site tests, refuses and fills it through
this module, so the marker's name and the layout the hydrator writes are
known here alone.
"""

from typing import Any

from pydantic_core import CoreSchema, core_schema

from .exceptions import InstanceNotLoaded

#: The `__dict__` key the hydrator sets on an unloaded instance (the Rust
#: `hydration::UNLOADED_MARKER`). Its value is `(model, path, found_no_row)`:
#: the model and relation path the instance was first read through, and
#: whether an include looked for its row and found none.
MARKER = "__ferro_unloaded"


def is_unloaded(instance: object) -> bool:
    """Whether `instance` holds only its primary key."""
    return MARKER in instance.__dict__


def require_loaded(instance: object) -> None:
    """Raise the refusal when `instance` is unloaded."""
    if MARKER in instance.__dict__:
        raise refusal(instance)


def _refuse_then_serialize(instance: Any, handler: Any) -> Any:
    require_loaded(instance)
    return handler(instance)


def guard_serialization(schema: CoreSchema) -> None:
    """Make a model's core schema refuse an unloaded instance before it serializes.

    The guard wraps whatever serializer the schema holds, a model's own
    `model_serializer` included, so every path through pydantic-core (a
    dump, a nested model, a `TypeAdapter`) refuses and no subclass can
    replace it. pydantic-core wraps the refusal in a
    `PydanticSerializationError` that carries its message. Wrapping is
    idempotent: a schema already guarded is left as it is.
    """
    if schema.get("type") != "model":
        return
    current = schema.get("serialization")
    if current is not None and current.get("function") is _refuse_then_serialize:
        return
    schema["serialization"] = core_schema.wrap_serializer_function_ser_schema(
        _refuse_then_serialize, schema={**schema}
    )


def refusal(instance: Any) -> InstanceNotLoaded:
    """The refusal of an unloaded instance, naming how to load it."""
    from .relations import _snake

    model, path, found_no_row = instance.__dict__[MARKER]
    cls = type(instance)
    pk = cls.__ferro_pk__
    held = f"{cls.__name__}({pk}={instance.__dict__[pk]!r}) is not loaded"
    parameter = _snake(model)
    relation = f"{parameter}.{path}"
    if found_no_row:
        return InstanceNotLoaded(
            f"{held}: the query that included {relation} found no row with that "
            "key, so the row was deleted or row security hides it from this "
            "connection; restore the row or this connection's access to it, "
            f"then await {relation}.load()"
        )
    return InstanceNotLoaded(
        f"{held}. Read it after include(lambda {parameter}: {relation}) or "
        f"await {relation}.load()"
    )


def repr_unloaded(instance: Any) -> str:
    """`Team(id=1, unloaded)`: the key an unloaded instance holds."""
    pk = type(instance).__ferro_pk__
    return f"{type(instance).__name__}({pk}={instance.__dict__[pk]!r}, unloaded)"


def fill(instance: Any, fetched: Any) -> None:
    """Copy the row `fetched` holds into `instance`, in place.

    `instance.__dict__` comes out as a fresh hydration lays it out: the
    declared fields in declaration order, then the connection and persisted
    markers, then what else it cached (its loaded relations). An unloaded
    instance is complete afterwards. The hydrator fills an instance the
    session holds the same way.
    """
    state = instance.__dict__
    kept = {
        name: value
        for name, value in state.items()
        if name != MARKER and name not in fetched.__dict__
    }
    state.clear()
    state.update(fetched.__dict__)
    state.update(kept)
    object.__setattr__(
        instance, "__pydantic_fields_set__", set(fetched.__pydantic_fields_set__)
    )
