"""Run every variant and print what each produced, then what ty says.

    uv run --no-sync python prototype/dto_declarations/show.py
"""

import importlib
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

VARIANTS = ["variant_a_markers", "variant_b_model_allowlist", "variant_c_resource_allowlist", "variant_d_handwritten", "variant_e_composed"]
SAMPLE = {"title": "Hello", "slug": "hello", "body": "Some words.", "is_featured": True, "api_token": "sk_live", "likes": 99}


def fields(cls) -> str:
    return ", ".join(f"{n}{'' if i.is_required() else '?'}" for n, i in cls.model_fields.items())


def main() -> None:
    results = {}
    for name in VARIANTS:
        module = importlib.import_module(name)
        results[name] = module
        lines = sum(1 for _ in open(HERE / f"{name}.py"))
        print(f"\n== {name}  ({lines} lines)")
        for role in ("ArticleCreate", "ArticleUpdate", "ArticleRead"):
            if hasattr(module, role):
                print(f"  {role}: {fields(getattr(module, role))}")
            else:
                print(f"  {role}: (none; E has no typed all-optional twin without writing it)")
        create, update = module.ArticleCreate, getattr(module, "ArticleUpdate", None)
        made = create.model_validate(SAMPLE)
        print(f"  create accepts {sorted(SAMPLE)} -> keeps {sorted(made.model_dump(exclude_unset=True))}")
        for label, payload in (("title of 121 chars", {**SAMPLE, "title": "x" * 121}), ("slug with a space", {**SAMPLE, "slug": "a b"}), ("reading_minutes=0", {**SAMPLE, "reading_minutes": 0})):
            try:
                create.model_validate(payload)
                print(f"  create {label}: ACCEPTED (constraint lost)")
            except ValidationError as error:
                print(f"  create {label}: refused ({error.errors()[0]['type']})")
        if update is not None:
            print(f"  update {{}} -> {update.model_validate({}).model_dump(exclude_unset=True)}")
    a, b, c, d, _e = (results[v] for v in VARIANTS)
    print("\n== variant D refuses a class that disagrees with the model")
    from pydantic import BaseModel
    from model import Article
    for body in ({"headline": str}, {"title": int}):
        try:
            d.dto(Article)(type("Bad", (BaseModel,), {"__annotations__": body}))
        except TypeError as error:
            print(f"  {error}")
    same = {role: len({frozenset(getattr(m, role).model_fields) for m in (a, b, c, d)}) == 1 for role in ("ArticleCreate", "ArticleUpdate", "ArticleRead")}
    print(f"\nall four variants produce the same field lists: {same}")
    print("\n== ty check (reveal_type of payload.title and payload.api_token in each handle())")
    out = subprocess.run(["uv", "run", "--no-sync", "ty", "check", "--output-format", "concise", str(HERE)], capture_output=True, text=True, cwd=HERE.parent.parent)
    for line in (out.stdout + out.stderr).splitlines():
        if "revealed-type" in line or "api_token" in line or "invalid-type-form" in line or line.startswith(("Found", "All")):
            print("  " + line.replace(str(HERE) + "/", ""))


if __name__ == "__main__":
    main()
