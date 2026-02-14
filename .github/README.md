# GitHub Actions Workflows

This directory contains the CI/CD workflows for automated releases, changelog generation, and PyPI publishing.

## Workflows

### 0. CI - Continuous Integration (`ci.yml`)

**Trigger:** Pull requests to `main`, push to `main`, manual dispatch
**Purpose:** Runs all quality checks on code changes

**What it does:**
- Runs all pre-commit hooks (includes Ruff, rustfmt, clippy, file checks)
- Runs pytest with coverage on multiple Python versions
- Runs Rust tests
- Builds package on all platforms (Linux, macOS, Windows)
- Checks conventional commit format on PRs
- Uploads coverage to Codecov

**Use case:** Ensuring code quality on every PR and commit

---

### 1. Update Changelog (`update-changelog.yml`)

**Trigger:** Push to `main` branch
**Purpose:** Automatically updates the `[Unreleased]` section of CHANGELOG.md

**What it does:**
- Runs when commits are pushed to main
- Analyzes conventional commits since last release
- Updates CHANGELOG.md with new entries in the `[Unreleased]` section
- Commits changes back to the repository

**Use case:** Keeps the changelog up-to-date as development progresses

---

### 2. Release (`release.yml`)

**Trigger:** GitHub release created
**Purpose:** Automates version bumping and release preparation

**What it does:**
- Runs when a new GitHub release is created
- Analyzes conventional commits to determine version bump
- Updates version in both `pyproject.toml` and `Cargo.toml`
- Moves `[Unreleased]` content to versioned section in CHANGELOG.md
- Creates a git tag (e.g., `v0.2.0`)
- Pushes commits and tags back to the repository
- Triggers the publish workflow

**Use case:** Creating a new release

---

### 3. Build & Publish (`publish.yml`)

**Trigger:** Release workflow completion OR manual dispatch
**Purpose:** Builds cross-platform wheels and publishes to PyPI

**What it does:**
- Builds wheels for multiple platforms using Maturin:
  - Linux (x86_64, aarch64)
  - macOS (x86_64, Apple Silicon)
  - Windows (x86_64)
- Runs tests on built wheels
- Publishes to PyPI using Trusted Publishing (no API tokens required)

**Use case:** Distributing new releases to PyPI

---

## Workflow Sequence

### Pull Request Flow
```
Developer opens PR
         ↓
    ci.yml (runs automatically)
    ├─ Pre-commit hooks (Ruff, rustfmt, clippy, file checks)
    ├─ Test Python (pytest, multiple versions)
    ├─ Test Rust (cargo test)
    ├─ Build check (all platforms)
    └─ Check conventional commits
         ↓
    All checks pass? → Merge PR
```

### Release Flow
```
Developer Push to main
         ↓
    ci.yml (runs on main)
    (All quality checks)
         ↓
    update-changelog.yml
    (Updates [Unreleased])
         ↓
    Developer creates GitHub release
         ↓
    release.yml
    (Bump version, tag, finalize changelog)
         ↓
    publish.yml
    (Build wheels, publish to PyPI)
```

## Manual Triggers

### Manually Trigger a Release

```bash
# Create a release using GitHub CLI
gh release create v0.2.0 --generate-notes

# Or use the GitHub web interface
```

### Manually Trigger Publishing

```bash
# Trigger the publish workflow
gh workflow run publish.yml
```

## Required Secrets & Permissions

### Repository Settings

1. **Actions Permissions:**
   - Settings → Actions → General → Workflow permissions
   - Enable: "Read and write permissions"
   - Enable: "Allow GitHub Actions to create and approve pull requests"

2. **PyPI Trusted Publishing:**
   - Go to PyPI → Manage Project → Publishing
   - Add GitHub as trusted publisher:
     - Owner: `syn54x`
     - Repository: `ferro-orm`
     - Workflow: `publish.yml`
     - Environment: `production` (optional)

### No Secrets Required!

All workflows use GitHub's built-in authentication (`${{ secrets.GITHUB_TOKEN }}`) and PyPI's Trusted Publishing. No manual secret management needed.

---

## Troubleshooting

### Changelog Not Updating

- Check that commits follow conventional commit format
- Verify workflow has write permissions
- Check workflow logs in Actions tab

### Release Failed

- Ensure all tests pass
- Verify both `pyproject.toml` and `Cargo.toml` have matching versions
- Check that the version hasn't already been released

### PyPI Publishing Failed

- Verify PyPI Trusted Publishing is configured correctly
- Check that the package version doesn't already exist on PyPI
- Review the build logs for compilation errors

---

## Local Testing

### Test Version Bump Locally

```bash
# Dry-run to see what version would be bumped to
uv run semantic-release version --print

# Test bump without pushing
uv run semantic-release version --no-push --no-tag --no-commit --skip-build
```

### Test Changelog Generation

```bash
# Generate changelog locally
uv run semantic-release changelog --unreleased
```

### Test Wheel Building

```bash
# Build wheels locally
uv run maturin build --release
```

---

## Maintenance

### Updating Workflow Versions

Workflows use pinned action versions (e.g., `actions/checkout@v4`). Update these periodically:

```bash
# Check for outdated actions
gh extension install github/gh-actions-updater
gh actions-updater .github/workflows/
```

### Monitoring

- Check the Actions tab regularly for failed workflows
- Set up notifications for workflow failures
- Review release notes after each automated release

---

**Last Updated:** 2026-01-27
