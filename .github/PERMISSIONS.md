# GitHub Actions Workflow Permissions

This document explains the fine-grained permissions used by each workflow in this repository.

## Overview

All workflows use explicit, fine-grained permissions (principle of least privilege). Each workflow only requests the permissions it needs to function.

**Repository Setting:** The repository-level "Workflow permissions" setting can remain at the default (read-only). Each workflow explicitly declares its required permissions.

---

## Workflow Permissions Breakdown

### 0. CI - Continuous Integration (`ci.yml`)

**Trigger:** Pull requests, push to `main`, manual dispatch

**Permissions:**
```yaml
# No explicit permissions needed
# Uses default read-only permissions
```

**Why These Permissions:**
- Default `contents: read` - Allows the workflow to:
  - Checkout code
  - Read repository contents
  - Run tests and linters
  - No write access needed

**What It Does:**
- Runs all pre-commit hooks (Ruff, rustfmt, clippy, file checks)
- Runs pytest with coverage on multiple Python versions
- Runs Rust tests
- Builds package on multiple platforms
- Checks conventional commit format on PRs
- Uploads coverage to Codecov

**Security:** Read-only access ensures CI cannot modify the repository.

---

### 1. Update Changelog (`update-changelog.yml`)

**Trigger:** Push to `main` branch

**Permissions:**
```yaml
permissions:
  contents: write
```

**Why These Permissions:**
- `contents: write` - Allows the workflow to:
  - Commit updated CHANGELOG.md back to the repository
  - Push changes to the `main` branch

**What It Does:**
- Reads conventional commits since last release
- Updates the `[Unreleased]` section of CHANGELOG.md
- Commits and pushes the updated changelog

---

### 2. Release (`release.yml`)

**Trigger:** Manual workflow dispatch OR release published

**Permissions:**
```yaml
permissions:
  contents: write
  issues: write
  pull-requests: write
```

**Why These Permissions:**
- `contents: write` - Allows the workflow to:
  - Commit version bumps to pyproject.toml and Cargo.toml
  - Push commits to the `main` branch
  - Create and push git tags (e.g., `v0.2.0`)
  - Create GitHub releases

- `issues: write` - Allows the workflow to:
  - Update issue references in release notes
  - Close issues automatically via commit messages
  - Add labels or comments to issues

- `pull-requests: write` - Allows the workflow to:
  - Update PR references in release notes
  - Close PRs automatically via commit messages
  - Add labels or comments to PRs

**What It Does:**
- Analyzes conventional commits
- Determines next version
- Updates version in both Python and Rust files
- Finalizes CHANGELOG.md
- Creates git tag
- Creates GitHub release
- Triggers publish workflow

---

### 3. Build & Publish (`publish.yml`)

**Trigger:** Workflow call, manual dispatch, or release published

**Permissions:**

**For build/test jobs:** (default - read-only)
```yaml
# No explicit permissions needed
# Uses default read permissions to:
# - Checkout code
# - Read repository contents
```

**For publish-pypi job:**
```yaml
permissions:
  id-token: write
```

**Why These Permissions:**
- `id-token: write` - Allows the workflow to:
  - Request an OIDC token from GitHub
  - Authenticate with PyPI using Trusted Publishing
  - Publish packages without API tokens

**What It Does:**
- Builds wheels for multiple platforms
- Builds source distribution
- Tests built packages
- Publishes to PyPI using OIDC authentication

---

## Permission Scopes Explained

### `contents: write`
Full access to repository contents, including:
- Committing files
- Pushing to branches
- Creating/deleting tags
- Creating releases

### `contents: read` (default)
Read-only access to repository contents:
- Cloning/checking out code
- Reading files
- Listing branches and tags

### `issues: write`
Permission to modify issues:
- Create, edit, close issues
- Add labels and assignees
- Add comments

### `pull-requests: write`
Permission to modify pull requests:
- Create, edit, close PRs
- Add labels and assignees
- Add comments
- Request reviewers

### `id-token: write`
Permission to request OIDC tokens:
- Get JWT token from GitHub
- Authenticate with external services (PyPI)
- No access to repository contents

---

## Security Best Practices

### ✅ Current Setup (Secure)

1. **Principle of Least Privilege**
   - Each workflow only requests permissions it needs
   - No workflows have more permissions than necessary

2. **Explicit Permissions**
   - All permissions are declared in workflow files
   - Easy to audit and review

3. **OIDC Authentication**
   - No long-lived API tokens
   - Tokens expire automatically
   - Tokens are tied to specific workflows

4. **Environment Protection** (publish workflow)
   - Uses `pypi` environment
   - Can require manual approval
   - Additional security layer

### ❌ What We're NOT Doing (Good!)

1. **Not using repository-wide write permissions**
   - Would give all workflows unnecessary access
   - Higher security risk

2. **Not using API tokens**
   - No secrets to manage
   - No token rotation needed

3. **Not granting `packages: write`**
   - Not needed for our use case
   - Reduces attack surface

---

## Troubleshooting

### Workflow Fails with "Permission Denied"

**Check:**
1. Permissions are declared in the workflow file
2. Organization doesn't block fine-grained permissions
3. Branch protection rules allow workflow commits

**Solution:**
- Verify the `permissions:` block exists in the workflow
- Check organization settings allow workflow permissions
- Add `permissions: {}` explicitly to override org defaults

### "Resource not accessible by integration"

**Cause:** Workflow trying to access resource without permission

**Solution:**
- Add the required permission to the workflow's `permissions:` block
- Common missing permissions:
  - `contents: write` for commits/tags
  - `pull-requests: write` for PR comments
  - `issues: write` for issue comments

### PyPI Publishing Fails with Authentication Error

**Cause:** Missing `id-token: write` permission

**Solution:**
- Ensure `publish-pypi` job has `id-token: write`
- Verify PyPI trusted publisher is configured correctly
- Check environment name matches (`pypi`)

---

## Verification

To verify permissions are working:

### Test Update Changelog
```bash
git commit --allow-empty -m "feat: test changelog workflow"
git push
# Check Actions tab - should see commit from github-actions[bot]
```

### Test Release
```bash
gh workflow run release.yml
# Check that version files are updated and tagged
```

### Test Publish
```bash
# Triggered automatically by release workflow
# Or manually: gh workflow run publish.yml
```

---

## GitHub Organization Settings

**If fine-grained permissions are blocked:**

1. Go to: https://github.com/organizations/syn54x/settings/actions
2. Under "Workflow permissions":
   - Enable "Read and write permissions" OR
   - Enable "Allow workflows to request permissions explicitly"
3. Save changes

**Current Status:** ✅ All workflows use explicit permissions and should work regardless of org defaults.

---

## Additional Resources

- [GitHub Actions Permissions](https://docs.github.com/en/actions/security-guides/automatic-token-authentication#permissions-for-the-github_token)
- [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
- [OIDC in GitHub Actions](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect)

---

**Last Updated:** 2026-01-27
**Status:** ✅ All workflows properly configured with fine-grained permissions
