# PyPI Trusted Publishing Setup Checklist

Use this checklist to track your progress setting up PyPI Trusted Publishing.

## Pre-Setup

- [ ] PyPI account created and email verified
- [ ] GitHub repository admin access confirmed
- [ ] Read PYPI_SETUP.md documentation

## PyPI Configuration

- [ ] Logged into https://pypi.org/
- [ ] Navigated to "Publishing" settings
- [ ] Added trusted publisher with correct details:
  - [ ] Owner: `syn54x`
  - [ ] Repository: `ferro-orm`
  - [ ] Workflow: `publish.yml`
  - [ ] Environment: `pypi` (optional)
- [ ] Verified configuration appears in publisher list

## GitHub Repository Setup

- [ ] Created GitHub environment named `pypi`
- [ ] Configured environment protection rules (optional):
  - [ ] Required reviewers
  - [ ] Wait timer
  - [ ] Branch restrictions (main only)
- [ ] Enabled workflow permissions:
  - [ ] Settings → Actions → General
  - [ ] "Read and write permissions" enabled
  - [ ] "Allow GitHub Actions to create and approve pull requests" enabled

## Workflow Verification

- [ ] Confirmed `.github/workflows/publish.yml` exists
- [ ] Verified workflow has `id-token: write` permission
- [ ] Verified workflow has `environment: pypi` (if using environment)
- [ ] All workflows pass pre-commit hooks

## Testing

- [ ] Test workflow triggered manually (optional)
- [ ] Reviewed workflow logs for authentication
- [ ] No OIDC errors in logs

## First Release Test

- [ ] Created test release
- [ ] Release workflow completed successfully
- [ ] Publish workflow completed successfully
- [ ] Package appears on PyPI
- [ ] Can install package: `pip install ferro-orm`

## Final Verification

- [ ] Tested on multiple platforms
- [ ] Documentation updated with install instructions
- [ ] Team members notified of new release process
- [ ] Test PyPI configured (optional)

---

## Quick Commands

```bash
# Manual workflow trigger
gh workflow run publish.yml

# Create a test release
gh release create v0.1.1 --generate-notes

# Check workflow status
gh run list --workflow=publish.yml

# Install and test
pip install ferro-orm
python -c "import ferro; print(ferro.__version__)"
```

---

## Need Help?

- See [PYPI_SETUP.md](./PYPI_SETUP.md) for detailed instructions
- Check [Actions tab](https://github.com/syn54x/ferro-orm/actions) for workflow logs
- Review [PyPI docs](https://docs.pypi.org/trusted-publishers/)

---

**Status:** ⏳ In Progress | ✅ Complete
**Date Started:** _______
**Date Completed:** _______
