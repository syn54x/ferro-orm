"""PROTOTYPE for #853. Throwaway: never merges.

The question: one 20-column model's create, update and read DTOs, declared
four ways. Which reads best, and what does `ty` see of each?

This is the model every variant starts from. `Article` has an autoincrement
key, a generated column, a secret, two privileged flags, a tenant column, two
foreign keys and timestamps. The roles every variant must express:

- create: title, slug, summary, body, status, category_id, reading_minutes, published_at
- update: the same fields, every one optional
- read: everything the client may see; not tenant_id, is_hidden, api_token, internal_notes
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import SecretStr

from ferro import BackRef, Field, ForeignKey, Model, Relation, fn


def utcnow() -> datetime:
    return datetime.now(UTC)


class ArticleStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    articles: Relation[list["Article"]] = BackRef()


class Category(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    articles: Relation[list["Article"]] = BackRef()


class Article(Model):
    id: int | None = Field(default=None, primary_key=True)
    tenant_id: UUID
    author: Annotated[Author, ForeignKey(related_name="articles")]
    category: Annotated[Category | None, ForeignKey(related_name="articles", nullable=True)] = None
    title: str = Field(max_length=120)
    slug: str = Field(unique=True, max_length=80, pattern=r"^[a-z0-9-]+$")
    summary: str | None = Field(default=None, max_length=300)
    body: str
    status: ArticleStatus = ArticleStatus.DRAFT
    word_count: int = Field(generated=lambda article: fn.length(article.body))
    reading_minutes: int = Field(default=1, ge=1)
    likes: int = 0
    is_featured: bool = False
    is_hidden: bool = False
    api_token: SecretStr
    internal_notes: str | None = None
    published_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    version: int = 1


COLUMNS = list(Article.model_fields)
assert len(COLUMNS) == 20, COLUMNS
