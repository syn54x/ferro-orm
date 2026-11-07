"""Variant B: an allowlist on the model, one lambda per role, in the style of
`__ferro_checks__`. A column nobody lists is in no DTO. The columns themselves
are declared exactly as today.
"""

from datetime import datetime
from typing import reveal_type, Annotated
from uuid import UUID

from pydantic import SecretStr

from _derive import derive, listed
from model import ArticleStatus, Author, Category, Field, ForeignKey, Model, Relation, fn, utcnow


class Article(Model):
    __ferro_table__ = "article_b"  # the prototype has four Articles in one process

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: UUID
    author: Annotated[Author, ForeignKey(related_name="articles_b")]
    category: Annotated[Category | None, ForeignKey(related_name="articles_b", nullable=True)] = None
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

    __ferro_dtos__ = {
        "create": lambda article: (
            article.title, article.slug, article.summary, article.body, article.status,
            article.category_id, article.reading_minutes, article.published_at,
        ),
        "update": lambda article: (
            article.title, article.slug, article.summary, article.body, article.status,
            article.category_id, article.reading_minutes, article.published_at,
        ),
        "read": lambda article: (
            article.id, article.author_id, article.category_id, article.title, article.slug,
            article.summary, article.body, article.status, article.word_count,
            article.reading_minutes, article.likes, article.is_featured, article.published_at,
            article.created_at, article.updated_at, article.version,
        ),
    }


roles = Article.__ferro_dtos__
ArticleCreate = derive(Article, "ArticleCreate", listed(Article, roles["create"]))
ArticleUpdate = derive(Article, "ArticleUpdate", listed(Article, roles["update"]), optional=True)
ArticleRead = derive(Article, "ArticleRead", listed(Article, roles["read"]), read=True)


def handle(payload: ArticleCreate) -> str:
    reveal_type(payload.title)
    reveal_type(payload.api_token)
    return payload.title
