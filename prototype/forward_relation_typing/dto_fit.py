"""How a read DTO built with plain Pydantic sees each option. Runs: no Ferro needed.

    uv run --no-sync python prototype/forward_relation_typing/dto_fit.py
"""

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


class TeamRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class PlayerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    team: TeamRead
    mentor: TeamRead | None


class Team:
    def __init__(self, id, name):
        self.id, self.name = id, name


class Handle:
    """Option C's handle: the value sits behind `.o`."""

    def __init__(self, value):
        self.o = value


class ForwardingHandle(Handle):
    """The same handle, also passing attribute reads through to the value."""

    def __getattr__(self, name):
        return getattr(self.o, name)


class Player:
    def __init__(self, team, mentor):
        self.id, self.team, self.mentor = 1, team, mentor


def attempt(label, build):
    try:
        print(f"{label:<58} {build().model_dump()}")
    except ValidationError as error:
        print(f"{label:<58} FAILS: {error.errors()[0]['loc']} {error.errors()[0]['type']}")


rustaceans = Team(7, "Rustaceans")

attempt("plain value (D, key-only Team), null mentor", lambda: PlayerRead.model_validate(Player(rustaceans, None)))
attempt("handle (C), null mentor", lambda: PlayerRead.model_validate(Player(Handle(rustaceans), Handle(None))))
attempt("forwarding handle (C), mentor set", lambda: PlayerRead.model_validate(Player(ForwardingHandle(rustaceans), ForwardingHandle(rustaceans))))
attempt("forwarding handle (C), null mentor", lambda: PlayerRead.model_validate(Player(ForwardingHandle(rustaceans), ForwardingHandle(None))))


class FerroRead(BaseModel):
    """What a Ferro-made read DTO could do: look through handles before validating."""

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, data):
        if isinstance(data, dict):
            return data
        return {name: (value.o if isinstance(value := getattr(data, name), Handle) else value) for name in cls.model_fields}


class PlayerReadFerro(FerroRead):
    id: int
    team: TeamRead
    mentor: TeamRead | None


attempt("handle (C) into a Ferro-made DTO, null mentor", lambda: PlayerReadFerro.model_validate(Player(Handle(rustaceans), Handle(None))))
