# PyPI Trusted Publishing Setup Guide

This guide will walk you through configuring PyPI Trusted Publishing for automated, secure package publishing from GitHub Actions.

## What is Trusted Publishing?

Trusted Publishing uses OpenID Connect (OIDC) to authenticate GitHub Actions workflows with PyPI. Instead of using API tokens, GitHub provides short-lived identity tokens that prove the workflow is authorized to publish your package.

**Benefits:**
- ✅ No API tokens to manage
- ✅ More secure (tokens expire quickly)
- ✅ No secrets in GitHub repository
- ✅ Automatic authentication
- ✅ Better audit trail

---

## Prerequisites

Before starting, ensure you have:

1. **A PyPI account**
   - Sign up at https://pypi.org/account/register/
   - Verify your email address

2. **The package name registered** (optional but recommended)
   - Reserve the name `ferro-orm` on PyPI
   - Prevents name squatting
   - Can be done during first publish

3. **Repository access**
   - Admin access to the GitHub repository
   - Ability to push to the `main` branch

---

## Step 1: Create Package on PyPI (Optional)

If you want to reserve the package name before publishing:

1. Go to https://pypi.org/
2. Click "Your account" → "Your projects"
3. Click "Submit new project"
4. Create a minimal `setup.py` or use Test PyPI first

**Alternative:** Skip this step and let the first publish create the package.

---

## Step 2: Configure Trusted Publisher on PyPI

### For a New Package (Not Yet Published)

1. **Go to PyPI Pending Publishers**
   - URL: https://pypi.org/manage/account/publishing/

2. **Click "Add a new pending publisher"**

3. **Fill in the form:**
   ```
   PyPI Project Name: ferro-orm
   Owner: syn54x
   Repository name: ferro-orm
   Workflow filename: release.yml
   Environment name: pypi (optional but recommended)
   ```
   **Important:** Use `release.yml`, not `publish.yml`. The release workflow calls publish.yml; PyPI's OIDC token reflects the top-level workflow (release.yml), so the trusted publisher must match.

4. **Click "Add"**

5. **Verify the configuration:**
   - Check that all fields are correct
   - Environment name matches your workflow (if specified)

### For an Existing Package

1. **Go to your project page**
   - URL: https://pypi.org/project/ferro-orm/

2. **Click "Manage" → "Publishing"**

3. **Scroll to "Trusted publishers"**

4. **Click "Add a new publisher"**

5. **Fill in the form:**
   ```
   Owner: syn54x
   Repository: ferro-orm
   Workflow: release.yml
   Environment: pypi (optional)
   ```
   Use `release.yml` (the workflow that triggers the run). Do not use `publish.yml` when it is invoked via workflow_call from release.yml, or uploads will fail with "Build Config URI does not match expected Trusted Publisher".

6. **Click "Add"**

---

## Step 3: Verify Configuration

After adding the trusted publisher, you should see:

```
✓ GitHub Actions workflow publisher configured

Owner:        syn54x
Repository:   ferro-orm
Workflow:     release.yml
Environment:  pypi
```

### Important Notes:

- **Owner** must match your GitHub username/organization
- **Repository** must match exactly (case-sensitive)
- **Workflow** must match the filename in `.github/workflows/`
- **Environment** is optional but provides additional security

---

## Step 4: Configure GitHub Environment (Recommended)

Using a GitHub environment adds an extra security layer with approval gates.

1. **Go to your GitHub repository**
   - URL: https://github.com/syn54x/ferro-orm

2. **Navigate to Settings → Environments**

3. **Click "New environment"**

4. **Name it `pypi`** (must match PyPI configuration)

5. **Configure protection rules (optional):**
   - ✅ Required reviewers: Add yourself or team members
   - ✅ Wait timer: Add a delay before publishing
   - ✅ Deployment branches: Restrict to `main` branch only

6. **Click "Save protection rules"**

### Why Use Environments?

- Adds manual approval step before publishing
- Prevents accidental publishes
- Better audit trail
- Can restrict to specific branches

---

## Step 5: Update GitHub Repository Permissions

Ensure GitHub Actions has the necessary permissions:

1. **Go to Settings → Actions → General**

2. **Under "Workflow permissions":**
   - ✅ Select "Read and write permissions"
   - ✅ Enable "Allow GitHub Actions to create and approve pull requests"

3. **Click "Save"**

---

## Step 6: Test the Setup

### Test with a Dry Run (Recommended)

Before doing a real release, test the workflow:

1. **Add a test script to your workflow:**
   ```yaml
   - name: Test PyPI authentication
     run: |
       echo "Testing PyPI OIDC authentication..."
       # This will verify OIDC token can be obtained
   ```

2. **Trigger the workflow manually:**
   ```bash
   gh workflow run release.yml
   ```

3. **Check the logs:**
   - Look for successful OIDC token exchange
   - Verify no authentication errors

### Full End-to-End Test

1. **Create a test release:**
   ```bash
   # Make a small change
   git commit -m "feat: test release automation"
   git push

   # Create a release
   gh release create v0.1.1 --generate-notes
   ```

2. **Monitor the workflows:**
   - Check "Actions" tab on GitHub
   - Watch the release workflow
   - Watch the publish workflow

3. **Verify on PyPI:**
   - Go to https://pypi.org/project/ferro-orm/
   - Check that the new version appears
   - Try installing: `pip install ferro-orm==0.1.1`

---

## Troubleshooting

### Error: "Certificate's Build Config URI ... does not match expected Trusted Publisher (publish.yml @ ...)"

**Cause:** PyPI Trusted Publishing uses the **top-level** workflow for OIDC. This repo runs `publish.yml` via `workflow_call` from `release.yml`, so the token identifies `release.yml`, not `publish.yml`. If PyPI is configured for `publish.yml`, uploads fail.

**Solution:** On PyPI, set the trusted publisher **Workflow** to `release.yml` (not `publish.yml`). Update or add the publisher: Owner `syn54x`, Repository `ferro-orm`, Workflow **release.yml**, Environment `pypi` (optional).

### Error: "Trusted publishing authentication failed"

**Cause:** OIDC token is rejected by PyPI

**Solutions:**
1. Verify all fields match exactly on PyPI:
   - Owner name (case-sensitive)
   - Repository name (case-sensitive)
   - Workflow filename: use **release.yml** (the workflow that starts the run)
   - Environment name (if specified)

2. Check GitHub environment exists:
   - Settings → Environments
   - Name must match PyPI configuration

3. Verify workflow syntax:
   - `environment: pypi` in the publish job
   - `permissions: id-token: write` is present

### Error: "Package already exists"

**Cause:** Version already published to PyPI

**Solutions:**
1. This is expected if re-running after success
2. Workflow has `skip-existing: true` to handle this
3. Bump version number for new releases

### Error: "Workflow not found"

**Cause:** PyPI can't find the workflow file

**Solutions:**
1. Ensure `release.yml` exists in `.github/workflows/` (this is the workflow PyPI should reference)
2. Verify the workflow filename on PyPI matches exactly (case-sensitive): `release.yml`
3. Check workflow is committed to `main` branch

### Error: "Environment not found"

**Cause:** GitHub environment doesn't exist

**Solutions:**
1. Create environment: Settings → Environments → New
2. Name must match PyPI configuration exactly
3. Or remove `environment:` from workflow if not using

### Error: "Permission denied"

**Cause:** Insufficient GitHub Actions permissions

**Solutions:**
1. Go to Settings → Actions → General
2. Enable "Read and write permissions"
3. Enable "Allow GitHub Actions to create and approve pull requests"

---

## Security Best Practices

### ✅ DO:
- Use GitHub environments for production publishing
- Require manual approval for releases
- Restrict deployments to `main` branch only
- Use environment-specific secrets for Test PyPI
- Review all workflow runs before approving

### ❌ DON'T:
- Use API tokens (Trusted Publishing is more secure)
- Allow workflows from pull requests to publish
- Skip environment protection on production
- Publish from feature branches
- Store credentials in repository secrets

---

## Test PyPI Setup (Optional)

For testing releases before production:

1. **Create Test PyPI account**
   - https://test.pypi.org/account/register/

2. **Configure trusted publisher on Test PyPI**
   - Same steps as above
   - Use separate workflow: `publish-test.yml`

3. **Create test workflow:**
   ```yaml
   - name: Publish to Test PyPI
     uses: pypa/gh-action-pypi-publish@release/v1
     with:
       repository-url: https://test.pypi.org/legacy/
       skip-existing: true
   ```

4. **Test installation:**
   ```bash
   pip install --index-url https://test.pypi.org/simple/ ferro-orm
   ```

---

## Verification Checklist

After completing setup, verify:

- [ ] PyPI trusted publisher configured
- [ ] GitHub environment `pypi` created (if using)
- [ ] Workflow permissions set to read/write
- [ ] Workflow file `release.yml` exists (trusted publisher must use this name)
- [ ] Workflow has `id-token: write` permission
- [ ] Test workflow runs successfully
- [ ] Can authenticate with PyPI (check logs)
- [ ] First package published successfully

---

## Additional Resources

- **PyPI Trusted Publishing Docs:** https://docs.pypi.org/trusted-publishers/
- **GitHub OIDC Docs:** https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect
- **Maturin Action:** https://github.com/PyO3/maturin-action
- **PyPI Publishing Action:** https://github.com/pypa/gh-action-pypi-publish

---

## Quick Reference

### PyPI Configuration
```
Project: ferro-orm
Owner: syn54x
Repository: ferro-orm
Workflow: release.yml
Environment: pypi (optional)
```

### GitHub Environment Settings
```
Name: pypi
Protection rules:
  - Required reviewers (optional)
  - Wait timer (optional)
  - Deployment branches: main only (recommended)
```

### Workflow Permissions Required
```yaml
permissions:
  id-token: write  # For OIDC token
  contents: write  # For creating releases
```

---

**Last Updated:** 2026-01-27
**Status:** Ready for Implementation
