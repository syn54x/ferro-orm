release:
    gh workflow run release.yml

prerelease:
    gh workflow run release.yml -f prerelease=true

docs:
    gh workflow run publish-docs.yml

test *ARGS:
    uv run pytest --db-backends=sqlite,postgres {{ARGS}}

check:
    uv run ty check src/ferro/query tests/test_query_typing.py tests/test_static_contracts.py

bench *ARGS:
    uv run python -m benchmarks.run {{ARGS}}
