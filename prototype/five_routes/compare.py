"""PROTOTYPE for #848. Run it:

    uv run --no-sync --with fastapi --with litestar --with httpx python prototype/five_routes/compare.py

It builds one FastAPI app and one Litestar app from the same plan, sends both
the same requests, writes both OpenAPI documents beside this file and prints
every place they differ.
"""

import asyncio
import json
import logging
import os
import re
import sys
from importlib.metadata import version
from pathlib import Path

import httpx

HERE = Path(__file__).parent
logging.getLogger("httpx").setLevel(logging.WARNING)
sys.path.insert(0, str(HERE))

import fastapi_adapter  # noqa: E402
import litestar_adapter  # noqa: E402
from players import PLAN  # noqa: E402

from ferro import connect  # noqa: E402

KEY = {"x-api-key": "let-me-in"}
REQUESTS = [
    ("POST", "/players", {"handle": "ada", "rating": 7, "team": "reds"}, KEY),
    ("POST", "/players", {"handle": "bo", "team": "blues"}, KEY),
    ("GET", "/players", None, KEY),
    ("GET", "/players?team=reds&limit=5", None, KEY),
    ("GET", "/players/{first}", None, KEY),
    ("PATCH", "/players/{first}", {"rating": 9}, KEY),
    ("DELETE", "/players/{first}", None, KEY),
    ("GET", "/players/{first}", None, KEY),  # gone: our 404
    ("GET", "/players", None, {}),  # no key: our 403
    ("POST", "/players", {"handle": "x"}, KEY),  # too short: the framework's own error
    ("GET", "/players?limit=0", None, KEY),  # bad query value
    ("GET", "/players/abc", None, KEY),  # key of the wrong type
    ("GET", "/players?nope=1", None, KEY),  # a query parameter nobody declared
    ("PATCH", "/players/{second}", {"rating": -1}, KEY),  # breaks ge=0 on a nullable field
]

# Why two documents built from one plan differ. First match wins.
CAUSES = [
    (r"HTTPValidationError|/ValidationError|/responses/(422|400)/",
     "Each framework documents its own validation error: 422 on FastAPI, 400 on Litestar"),
    (r"/parameters$",
     "Parameters are compared whole: titles, anyOf against oneOf, Litestar's extra flags, 1 against 1.0"),
    (r"/PlayerUpdate/properties/\w+/(anyOf|oneOf|minLength|maxLength|minimum)$",
     "A nullable field: FastAPI writes anyOf with the limits inside, Litestar oneOf with the limits beside it"),
    (r"(/properties/\w+|/schema)/title$", "FastAPI titles every property and list response, Litestar only the class"),
    (r"/components/schemas/\w+/description$", "FastAPI copies the class docstring, Litestar does not"),
    (r"/required$", "Litestar sorts `required` and writes it when empty"),
    (r"/responses/\d+/description$", "Each framework has its own wording for a success response"),
    (r"/deprecated$", "Litestar writes `deprecated: false` on every operation"),
    (r"^/servers", "Litestar adds a `servers` entry"),
]


async def drive(app) -> list[tuple[str, int, str]]:
    out = []
    first = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        for method, path, body, headers in REQUESTS:
            response = await client.request(method, path.replace('{first}', str(first)).replace('{second}', str((first or 0) + 1)), json=body, headers=headers)
            if first is None:
                first = response.json()["id"]
            # both apps share one table, so print keys as if each started at 1
            text = re.sub(r'(id"?:? ?)(\d+)', lambda m: m[1] + str(int(m[2]) - first + 1), response.text)
            out.append((f"{method} {path}", response.status_code, text if len(text) < 150 else text[:147] + "..."))
        # leave the table as we found it for the next app
        for row in (await client.get("/players", headers=KEY)).json():
            await client.delete(f"/players/{row['id']}", headers=KEY)
    return out


def flatten(node, path=""):
    if isinstance(node, dict):
        for key in node:
            yield from flatten(node[key], f"{path}/{key.replace('/', '~')}")
    elif isinstance(node, list):
        yield path, json.dumps(node, sort_keys=True)
    else:
        yield path, json.dumps(node)


async def main() -> None:
    print(f"fastapi {version('fastapi')}  litestar {version('litestar')}  pydantic {version('pydantic')}\n")
    await connect("sqlite::memory:", auto_migrate=True)
    apps = {"fastapi": fastapi_adapter.build_app(PLAN), "litestar": litestar_adapter.build_app(PLAN)}

    runs = {name: await drive(app) for name, app in apps.items()}
    print("== The same requests, both apps ==")
    for (label, f_status, f_body), (_, l_status, l_body) in zip(runs["fastapi"], runs["litestar"]):
        same = "same" if (f_status, f_body) == (l_status, l_body) else "DIFFERENT"
        print(f"\n{label}  [{same}]")
        if same == "same":
            print(f"  both      {f_status} {f_body}")
        else:
            print(f"  fastapi   {f_status} {f_body}")
            print(f"  litestar  {l_status} {l_body}")

    docs = {"fastapi": apps["fastapi"].openapi(), "litestar": apps["litestar"].openapi_schema.to_schema()}
    suffix = "-" + os.environ["VARIANT"] if os.environ.get("VARIANT") else ""
    for name, doc in docs.items():
        (HERE / f"openapi-{name}{suffix}.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")

    flat = {name: dict(flatten(doc)) for name, doc in docs.items()}
    paths = sorted(set(flat["fastapi"]) | set(flat["litestar"]))
    different = [p for p in paths if flat["fastapi"].get(p) != flat["litestar"].get(p)]
    print(f"\n== The two OpenAPI documents: {len(paths) - len(different)} values agree, {len(different)} differ ==")
    by_cause: dict[str, list[str]] = {}
    for p in different:
        cause = next((label for pattern, label in CAUSES if re.search(pattern, p)), "UNEXPLAINED")
        by_cause.setdefault(cause, []).append(p)
    for cause, found in sorted(by_cause.items(), key=lambda item: -len(item[1])):
        example = found[0]
        print(f"\n{len(found):>3}  {cause}")
        print(f"     e.g. {example}")
        print(f"       fastapi   {flat['fastapi'].get(example, '(absent)')[:160]}")
        print(f"       litestar  {flat['litestar'].get(example, '(absent)')[:160]}")
    (HERE / f"diff{suffix}.txt").write_text("".join(
        f"{p}\n  fastapi   {flat['fastapi'].get(p, '(absent)')}\n  litestar  {flat['litestar'].get(p, '(absent)')}\n\n"
        for p in different))
    print(f"\nEvery difference is in diff{suffix}.txt, beside the two openapi-*{suffix}.json documents.")

asyncio.run(main())
