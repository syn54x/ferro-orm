"""PROTOTYPE for #848. Everything Litestar makes the adapter own."""

from inspect import Parameter, Signature
from typing import Annotated

from litestar import Litestar, Request, Response, Router
from litestar.connection import ASGIConnection
from litestar.exceptions import ValidationException
from litestar.handlers import BaseRouteHandler, HTTPRouteHandler
from litestar.openapi import OpenAPIConfig
from litestar.openapi.datastructures import ResponseSpec
from litestar.params import FromPath, JSONBody, QueryParameter

from plan import ApiError, ErrorBody, Invalid, Operation, ResourcePlan, run

_PATH_TYPES = {int: "int", str: "str"}


def _handler(plan: ResourcePlan, operation: Operation) -> HTTPRouteHandler:
    # Litestar reads the signature and the annotations, wants the body named
    # `data`, and refuses a handler with no return annotation. 2.25 deprecates
    # guessing where a parameter comes from, so each one carries its marker.
    params = []
    if operation.takes_key:
        params.append(Parameter(plan.key_name, Parameter.KEYWORD_ONLY, annotation=FromPath[plan.key_type]))
    if operation.body:
        params.append(Parameter("data", Parameter.KEYWORD_ONLY, annotation=JSONBody[operation.body]))
    for q in operation.query:
        marker = QueryParameter(description=q.description, ge=q.ge, le=q.le)
        params.append(Parameter(q.name, Parameter.KEYWORD_ONLY, default=q.default,
                                annotation=Annotated[q.type, marker]))

    async def handler(**kwargs):
        key = kwargs.pop(plan.key_name, None) if operation.takes_key else None
        return await run(plan, operation, key=key, body=kwargs.pop("data", None), query=kwargs)

    handler.__signature__ = Signature(params, return_annotation=operation.returns)
    handler.__annotations__ = {p.name: p.annotation for p in params} | {"return": operation.returns}
    handler.__name__ = operation.operation_id

    # The path names the key's type: /{id:int}.
    typed_key = "{" + f"{plan.key_name}:{_PATH_TYPES[plan.key_type]}" + "}"
    return HTTPRouteHandler(
        operation.path.replace("{key}", typed_key) or "/",
        http_method=operation.method,
        status_code=operation.status,
        summary=operation.summary,
        operation_id=operation.operation_id,
        responses={error.status: ResponseSpec(data_container=ErrorBody, description=error.code,
                                             generate_examples=False)
                   for error in operation.errors},
    )(handler)


def build_router(plan: ResourcePlan) -> Router:
    guards = []
    if plan.guard:
        def guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:
            plan.guard(connection.headers)

        guards.append(guard)

    def on_api_error(request: Request, error: ApiError) -> Response:
        return Response(error.body().model_dump(), status_code=error.status)

    handlers = {ApiError: on_api_error}  # a Router can hold these
    if plan.one_validation_error:
        def on_invalid(request: Request, error: ValidationException) -> Response:
            first = error.extra[0]
            return on_api_error(request, Invalid(f"{first.get('key')}: {first['message']}"))

        handlers[ValidationException] = on_invalid

    return Router(
        plan.prefix,
        route_handlers=[_handler(plan, operation) for operation in plan.operations],
        tags=[plan.tag],
        guards=guards,
        exception_handlers=handlers,
    )


def build_app(plan: ResourcePlan) -> Litestar:
    return Litestar(
        [build_router(plan)],
        openapi_config=OpenAPIConfig(title="Players", version="1"),
    )
