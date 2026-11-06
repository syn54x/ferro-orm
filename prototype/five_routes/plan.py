"""PROTOTYPE for #848. Throwaway: never merges.

The question: five routes for one model on FastAPI and on Litestar, generated
from one plan. Are the two OpenAPI documents identical, and what does each
adapter have to own?

This file is the plan and the framework-free half: what the five routes are,
and what each one does. It imports neither framework.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ferro import ModelDoesNotExist, engines


class ErrorBody(BaseModel):
    """The one error body both adapters send."""

    code: str
    detail: str


class ApiError(Exception):
    status = 500
    code = "error"

    def body(self) -> ErrorBody:
        return ErrorBody(code=self.code, detail=str(self))


class NotFound(ApiError):
    status = 404
    code = "not_found"


class Forbidden(ApiError):
    status = 403
    code = "forbidden"


class Invalid(ApiError):
    """A request the framework refused before the route ran."""

    status = 422
    code = "invalid"


@dataclass(frozen=True)
class QueryParam:
    name: str
    type: Any
    default: Any
    description: str
    ge: int | None = None
    le: int | None = None


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str  # "" or "/{key}", in neither framework's syntax
    status: int
    summary: str
    operation_id: str
    returns: Any
    body: type[BaseModel] | None = None
    takes_key: bool = False
    query: tuple[QueryParam, ...] = ()
    errors: tuple[type[ApiError], ...] = ()


@dataclass(frozen=True)
class ResourcePlan:
    model: type
    prefix: str
    tag: str
    create: type[BaseModel]
    update: type[BaseModel]
    read: type[BaseModel]
    key_name: str = "id"
    key_type: type = int
    filters: tuple[tuple[str, type], ...] = ()
    # A per-request check over the request headers. Raises Forbidden.
    guard: Callable[[Mapping[str, str]], None] | None = None
    # Variant: both adapters turn the framework's validation error into Invalid.
    one_validation_error: bool = False
    operations: tuple[Operation, ...] = field(init=False)

    def __post_init__(self) -> None:
        noun = self.model.__name__.lower()
        guarded = ((Forbidden,) if self.guard else ()) + ((Invalid,) if self.one_validation_error else ())
        equals = tuple(
            QueryParam(name, type_ | None, None, f"Only rows whose {name} equals this.")
            for name, type_ in self.filters
        )
        limit = QueryParam("limit", int, 50, "The most rows to return.", ge=1, le=100)
        operations = (
            Operation("list", "GET", "", 200, f"List {noun}s", f"list_{noun}s",
                      list[self.read], query=(*equals, limit), errors=guarded),
            Operation("get", "GET", "/{key}", 200, f"Get one {noun}", f"get_{noun}",
                      self.read, takes_key=True, errors=(*guarded, NotFound)),
            Operation("create", "POST", "", 201, f"Create a {noun}", f"create_{noun}",
                      self.read, body=self.create, errors=guarded),
            Operation("update", "PATCH", "/{key}", 200, f"Update a {noun}", f"update_{noun}",
                      self.read, body=self.update, takes_key=True, errors=(*guarded, NotFound)),
            Operation("delete", "DELETE", "/{key}", 204, f"Delete a {noun}", f"delete_{noun}",
                      None, takes_key=True, errors=(*guarded, NotFound)),
        )
        object.__setattr__(self, "operations", operations)


async def run(
    plan: ResourcePlan,
    operation: Operation,
    *,
    key: Any = None,
    body: BaseModel | None = None,
    query: Mapping[str, Any] | None = None,
) -> Any:
    """Do one operation. Every call opens its own session."""
    model, read = plan.model, plan.read
    async with engines.session():
        if operation.name == "list":
            query = dict(query or {})
            rows = model.select().order_by(plan.key_name).limit(query.pop("limit"))
            for name, value in query.items():
                if value is not None:
                    rows = rows.where(lambda row, name=name, value=value: getattr(row, name) == value)
            return [read.model_validate(row) for row in await rows.all()]
        if operation.name == "create":
            return read.model_validate(await model.create(**body.model_dump()))
        try:
            row = await model.get(key)
        except ModelDoesNotExist:
            raise NotFound(f"No {model.__name__.lower()} has {plan.key_name} {key}.") from None
        if operation.name == "get":
            return read.model_validate(row)
        if operation.name == "update":
            for name, value in body.model_dump(exclude_unset=True).items():
                setattr(row, name, value)
            await row.save()
            return read.model_validate(row)
        await row.delete()
        return None
