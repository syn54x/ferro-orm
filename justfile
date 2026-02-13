release:
    gh workflow run release.yml

prerelease:
    gh workflow run release.yml -f prerelease=true

docs:
    gh workflow run publish-docs.yml
