"""PROTOTYPE for #848. One model, three hand-written classes, one plan."""

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from ferro import Field, Model

from plan import Forbidden, ResourcePlan


class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    handle: str = Field(unique=True)
    rating: int = 0
    team: str = "free agents"


class PlayerCreate(BaseModel):
    handle: str = PydanticField(min_length=2, max_length=40)
    rating: int = PydanticField(default=0, ge=0)
    team: str = "free agents"


class PlayerUpdate(BaseModel):
    handle: str | None = PydanticField(default=None, min_length=2, max_length=40)
    rating: int | None = PydanticField(default=None, ge=0)
    team: str | None = None


class PlayerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    handle: str
    rating: int
    team: str


def require_key(headers: Mapping[str, str]) -> None:
    if headers.get("x-api-key") != "let-me-in":
        raise Forbidden("Send the x-api-key header.")


PLAN = ResourcePlan(
    model=Player,
    prefix="/players",
    tag="players",
    create=PlayerCreate,
    update=PlayerUpdate,
    read=PlayerRead,
    filters=(("team", str), ("rating", int)),
    guard=require_key,
    one_validation_error=os.environ.get("VARIANT") == "one-error",
)
