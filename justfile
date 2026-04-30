release:
    gh workflow run release.yml

prerelease:
    gh workflow run release.yml -f prerelease=true

docs:
    gh workflow run publish-docs.yml

test *ARGS:
    uv run pytest --db-backends=sqlite,postgres {{ARGS}}
