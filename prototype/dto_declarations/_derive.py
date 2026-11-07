"""The one derivation the first three variants share: a Pydantic class built
from a model's own FieldInfo objects (#847: constraints, aliases, defaults and
SecretStr carry over this way)."""

import copy
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model



def derive(model: Any, name: str, fields: Iterable[str], *, optional: bool = False, read: bool = False) -> type[BaseModel]:
    pairs = {}
    for field in fields:
        info = copy.deepcopy(model.model_fields[field])
        annotation = info.annotation
        if optional:
            info.default, info.default_factory = None, None
            annotation = annotation | None  # type: ignore[operator]
        pairs[field] = (annotation, info)
    config = ConfigDict(from_attributes=True) if read else None
    return create_model(name, __config__=config, **pairs)


class Columns:
    """What the lambda in an allowlist receives: `article.title` is the name `"title"`."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def __getattr__(self, name: str) -> str:
        if name not in self._model.model_fields:
            raise AttributeError(f"{self._model.__name__} has no column {name!r}")
        return name


def listed(model: Any, pick: Any) -> list[str]:
    chosen = pick(Columns(model))
    return [chosen] if isinstance(chosen, str) else list(chosen)
