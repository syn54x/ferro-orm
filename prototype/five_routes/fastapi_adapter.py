"""PROTOTYPE for #848. Everything FastAPI makes the adapter own."""

from inspect import Parameter, Signature
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from plan import ApiError, ErrorBody, Invalid, Operation, ResourcePlan, run


def _handler(plan: ResourcePlan, operation: Operation):
    # FastAPI reads a handler's parameters from its signature, so the adapter
    # writes one: the key, the body (any name), then the query parameters.
    params = []
    if operation.takes_key:
        params.append(Parameter(plan.key_name, Parameter.KEYWORD_ONLY, annotation=plan.key_type))
    if operation.body:
        params.append(Parameter("body", Parameter.KEYWORD_ONLY, annotation=operation.body))
    for q in operation.query:
        marker = Query(description=q.description, ge=q.ge, le=q.le)
        params.append(Parameter(q.name, Parameter.KEYWORD_ONLY, default=q.default,
                                annotation=Annotated[q.type, marker]))

    async def handler(**kwargs):
        key = kwargs.pop(plan.key_name, None) if operation.takes_key else None
        return await run(plan, operation, key=key, body=kwargs.pop("body", None), query=kwargs)

    handler.__signature__ = Signature(params)
    handler.__name__ = operation.operation_id
    return handler


def build_router(plan: ResourcePlan) -> APIRouter:
    dependencies = []
    if plan.guard:
        async def guard(request: Request) -> None:
            plan.guard(request.headers)

        dependencies.append(Depends(guard))

    router = APIRouter(prefix=plan.prefix, tags=[plan.tag], dependencies=dependencies)
    for operation in plan.operations:
        router.add_api_route(
            operation.path.replace("{key}", "{" + plan.key_name + "}"),
            _handler(plan, operation),
            methods=[operation.method],
            status_code=operation.status,
            response_model=operation.returns,
            summary=operation.summary,
            operation_id=operation.operation_id,
            responses={error.status: {"model": ErrorBody, "description": error.code}
                       for error in operation.errors},
        )
    return router


def install_errors(app: FastAPI, plan: ResourcePlan) -> None:
    # An APIRouter cannot hold exception handlers; only the app can.
    async def on_api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content=error.body().model_dump())

    app.add_exception_handler(ApiError, on_api_error)

    if plan.one_validation_error:
        async def on_invalid(request: Request, error: RequestValidationError) -> JSONResponse:
            first = error.errors()[0]
            return await on_api_error(request, Invalid(f"{first['loc'][-1]}: {first['msg']}"))

        app.add_exception_handler(RequestValidationError, on_invalid)


def build_app(plan: ResourcePlan) -> FastAPI:
    app = FastAPI(title="Players", version="1")
    app.include_router(build_router(plan))
    install_errors(app, plan)
    return app
