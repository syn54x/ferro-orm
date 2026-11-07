"""Variant E: the model is composed from its DTOs.

Today's metaclass finds a relation only in the model's own class body, not in
a mixin, so the key columns here are written as plain columns (the shape the
explicit-key spec gives them) and the relations are left off.
 Each group of fields is
written once as a plain Pydantic class; the DTOs are those classes (and unions
of them by inheritance); the table is the class that inherits them all.
"""

from datetime import datetime
from typing import reveal_type
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr

from model import ArticleStatus, Field, Model, fn, utcnow


class ArticleWritable(BaseModel):
    """What a client may send: the create DTO."""

    title: str = Field(max_length=120)
    slug: str = Field(unique=True, max_length=80, pattern=r"^[a-z0-9-]+$")
    summary: str | None = Field(default=None, max_length=300)
    body: str
    status: ArticleStatus = ArticleStatus.DRAFT
    category_id: int | None = None  # spec 1 form: Field(foreign_key=Category); the relation lives on the model
    reading_minutes: int = Field(default=1, ge=1)
    published_at: datetime | None = None


class ArticleServerWritten(BaseModel):
    """What a client may see but never send."""

    id: int | None = Field(default=None, primary_key=True)
    author_id: int  # spec 1 form: Field(foreign_key=Author); the relation lives on the model
    word_count: int = Field(generated=lambda article: fn.length(article.body))
    likes: int = 0
    is_featured: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    version: int = 1


class ArticleReadable(ArticleWritable, ArticleServerWritten):
    """The read DTO: everything a client may see."""

    model_config = ConfigDict(from_attributes=True)


class ArticlePrivate(BaseModel):
    """What never leaves the server."""

    tenant_id: UUID
    is_hidden: bool = False
    api_token: SecretStr
    internal_notes: str | None = None


class Article(Model, ArticleReadable, ArticlePrivate):
    """The table."""

    __ferro_table__ = "article_e"


ArticleCreate = ArticleWritable
ArticleRead = ArticleReadable


def handle(payload: ArticleCreate) -> str:
    reveal_type(payload.title)
    reveal_type(payload.api_token)
    return payload.title
