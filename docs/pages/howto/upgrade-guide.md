<!--
  MAINTAINERS: This is the single, cumulative upgrade guide. When a release
  introduces breaking changes, ADD a new "## Upgrading to X.Y" section at the
  TOP of the boundary list (below "Before you start") — do NOT create a new
  per-version page. Keep each section's copy/paste agent prompt in sync with the
  prose directly beneath it.
-->

# Upgrade Guide

This is the curated path for upgrading Ferro across releases that changed
behavior. The [changelog](../changelog.md) is the exhaustive per-version record;
this guide is the task-oriented "here is the work" companion for the boundaries
that need it.

Find your current version below, then work **top to bottom** through every
boundary section you cross. Each section describes the settled end state — what
the API does now, and what you change to get there.

## Which sections apply to you

| You are on | Read |
| --- | --- |
| `0.13.x`, upgrading to `0.14` | [Upgrading to 0.14](#upgrading-to-014) |
| `0.11.x` or earlier, upgrading to `0.14` | Both sections, top to bottom |
| `0.11.x` or earlier, stopping at `0.12`/`0.13` | [Upgrading to 0.12](#upgrading-to-012) |

Each section includes a copy/paste prompt for a coding agent. If you are crossing
more than one boundary, run the prompts **newest first, one at a time**, and
verify your suite between them.

## Before you start

Turn deprecation warnings into failures on a throwaway branch so nothing slips
through:

```bash
uv run pytest -W error::DeprecationWarning
```

This catches every **deprecation-based** change. It does **not** catch the silent
behavioral changes in `0.14` (`create()`/`save()` no longer upsert;
`limit()`/`offset()` on a mutation now raise) — those emit no warning and must be
reviewed by reading the relevant section below.
