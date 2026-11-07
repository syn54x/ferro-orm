"""Variant C: the same allowlist at the call site, on the resource object. The
model says nothing about DTOs; the API that serves it does. Two APIs over one
model (an admin API and a public one) each write their own lists.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from typing import reveal_type

from pydantic import BaseModel

from _derive import derive, listed
from model import Article


@dataclass
class Resource:
    model: type
    create: Callable
    update: Callable
    read: Callable
    Create: type[BaseModel] = field(init=False)
    Update: type[BaseModel] = field(init=False)
    Read: type[BaseModel] = field(init=False)

    def __post_init__(self) -> None:
        name = self.model.__name__
        self.Create = derive(self.model, f"{name}Create", listed(self.model, self.create))
        self.Update = derive(self.model, f"{name}Update", listed(self.model, self.update), optional=True)
        self.Read = derive(self.model, f"{name}Read", listed(self.model, self.read), read=True)


WRITABLE = lambda article: (  # noqa: E731
    article.title, article.slug, article.summary, article.body, article.status,
    article.category_id, article.reading_minutes, article.published_at,
)

articles = Resource(
    Article,
    create=WRITABLE,
    update=WRITABLE,
    read=lambda article: (
        article.id, article.author_id, article.category_id, article.title, article.slug,
        article.summary, article.body, article.status, article.word_count,
        article.reading_minutes, article.likes, article.is_featured, article.published_at,
        article.created_at, article.updated_at, article.version,
    ),
)

ArticleCreate, ArticleUpdate, ArticleRead = articles.Create, articles.Update, articles.Read


def handle(payload: ArticleCreate) -> str:
    reveal_type(payload.title)
    reveal_type(payload.api_token)
    return payload.title
