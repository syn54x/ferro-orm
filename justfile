# Cut a release. Pass optional markdown to prepend to the top of the GitHub
# Release body (the auto-generated changelog stays beneath it):
#
#   just release "## Query joins have landed 🎉  See the [guide](https://…)."
#   just release "$(cat /tmp/notes.md)"
#   just release $'## Query joins\n\nTraverse relations in `where()` …\n\nDocs: https://…'
release notes="":
    gh workflow run release.yml -f highlights={{ quote(notes) }}

prerelease notes="":
    gh workflow run release.yml -f prerelease=true -f highlights={{ quote(notes) }}

docs:
    gh workflow run publish-docs.yml

test *ARGS:
    uv run pytest --db-backends=sqlite,postgres {{ARGS}}

check:
    uv run ty check src/ferro/query tests/test_query_typing.py tests/test_static_contracts.py

bench *ARGS:
    uv run python -m benchmarks.run {{ARGS}}
