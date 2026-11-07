"""Variant D: hand-written Pydantic classes that Ferro checks against the model.
The class names its fields and their types; Ferro refuses a field the model
lacks or types differently, and copies the model's constraints (max_length,
pattern, ge) onto the class, so the class does not repeat them.

Prototype note: `@dto(Article)` rebuilds the class in place with the model's
FieldInfo merged in; its return type is the class it was given, so a checker
keeps the class.
"""

from datetime import datetime
from typing import reveal_type, TypeVar

from pydantic import BaseModel, ConfigDict, create_model

from model import Article, ArticleStatus

T = TypeVar("T", bound=BaseModel)


def dto(model: type[BaseModel], *, optional: bool = False, read: bool = False):
    def check(cls: type[T]) -> type[T]:
        pairs = {}
        for name, declared in cls.model_fields.items():
            if name not in model.model_fields:
                raise TypeError(f"{cls.__name__}.{name}: {model.__name__} has no column {name!r}")
            info = model.model_fields[name]
            expected = info.annotation | None if optional else info.annotation
            if declared.annotation != expected:
                raise TypeError(
                    f"{cls.__name__}.{name} is {declared.annotation!r}; {model.__name__}.{name} is "
                    f"{info.annotation!r}. Write `{name}: {expected!r}`."
                )
            merged = info._copy() if hasattr(info, "_copy") else type(info).merge_field_infos(info)
            merged.annotation = declared.annotation
            if optional:
                merged.default, merged.default_factory = None, None
            pairs[name] = (declared.annotation, merged)
        config = ConfigDict(from_attributes=True) if read else None
        return create_model(cls.__name__, __config__=config, __module__=cls.__module__, **pairs)  # ty: ignore[no-matching-overload]

    return check


@dto(Article)
class ArticleCreate(BaseModel):
    title: str
    slug: str
    summary: str | None
    body: str
    status: ArticleStatus
    category_id: int | None
    reading_minutes: int
    published_at: datetime | None


@dto(Article, optional=True)
class ArticleUpdate(BaseModel):
    title: str | None
    slug: str | None
    summary: str | None
    body: str | None
    status: ArticleStatus | None
    category_id: int | None
    reading_minutes: int | None
    published_at: datetime | None


@dto(Article, read=True)
class ArticleRead(BaseModel):
    id: int | None
    author_id: int | None
    category_id: int | None
    title: str
    slug: str
    summary: str | None
    body: str
    status: ArticleStatus
    word_count: int
    reading_minutes: int
    likes: int
    is_featured: bool
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


def handle(payload: ArticleCreate) -> str:
    reveal_type(payload.title)
    reveal_type(payload.api_token)  # not in the DTO; ty should refuse this line
    return payload.title
