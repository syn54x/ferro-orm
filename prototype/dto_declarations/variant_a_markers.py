"""Variant A: markers on the field. Every column is in every DTO unless its
Field says otherwise: `writable=False` keeps it out of create and update,
`private=True` keeps it out of all three. The key and the generated column
need no marker; Ferro knows they are never written.

Prototype note: Ferro's `Field` has no such options yet, so a local wrapper
hangs the marker on the FieldInfo. The spelling is the thing under judgement.
"""

from datetime import datetime
from typing import reveal_type, Annotated
from uuid import UUID

import ferro
from pydantic import SecretStr

from _derive import derive
from model import ArticleStatus, Author, Category, ForeignKey, Model, Relation, fn, utcnow


class _Marker:
    def __init__(self, writable: bool, private: bool) -> None:
        self.writable, self.private = writable, private


def Field(*args, writable: bool = True, private: bool = False, **kwargs):  # noqa: N802
    info = ferro.Field(*args, **kwargs)
    info.metadata.append(_Marker(writable, private))
    return info


class Article(Model):
    __ferro_table__ = "article_a"  # the prototype has four Articles in one process

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: UUID = Field(writable=False, private=True)
    author: Annotated[Author, ForeignKey(related_name="articles_a")]
    category: Annotated[Category | None, ForeignKey(related_name="articles_a", nullable=True)] = None
    title: str = Field(max_length=120)
    slug: str = Field(unique=True, max_length=80, pattern=r"^[a-z0-9-]+$")
    summary: str | None = Field(default=None, max_length=300)
    body: str
    status: ArticleStatus = ArticleStatus.DRAFT
    word_count: int = Field(generated=lambda article: fn.length(article.body))
    reading_minutes: int = Field(default=1, ge=1)
    likes: int = Field(default=0, writable=False)
    is_featured: bool = Field(default=False, writable=False)
    is_hidden: bool = Field(default=False, private=True)
    api_token: SecretStr = Field(private=True)
    internal_notes: str | None = Field(default=None, private=True)
    published_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, writable=False)
    updated_at: datetime = Field(default_factory=utcnow, writable=False)
    version: int = Field(default=1, writable=False)


# `author_id` is server-written (the signed-in user), which a marker on `author`
# cannot say today: the forward relation's Field is the ForeignKey annotation.
SERVER_WRITTEN_KEYS = {"author_id"}


def _marker(info) -> _Marker:
    return next((m for m in info.metadata if isinstance(m, _Marker)), _Marker(True, False))


def _never_written(name: str, info) -> bool:
    extra = (info.json_schema_extra or {}).get("ferro_field", {})
    generated = any(type(m).__name__ == "GeneratedExpression" for m in info.metadata)
    return bool(extra.get("primary_key")) or generated or name in SERVER_WRITTEN_KEYS


writable = [n for n, i in Article.model_fields.items() if _marker(i).writable and not _marker(i).private and not _never_written(n, i)]
readable = [n for n, i in Article.model_fields.items() if not _marker(i).private]

ArticleCreate = derive(Article, "ArticleCreate", writable)
ArticleUpdate = derive(Article, "ArticleUpdate", writable, optional=True)
ArticleRead = derive(Article, "ArticleRead", readable, read=True)


def handle(payload: ArticleCreate) -> str:
    reveal_type(payload.title)  # what ty sees
    reveal_type(payload.api_token)  # not in the DTO; a typed form flags it
    return payload.title
