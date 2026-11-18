"""Packaging Smoke runs its macos job on a release or by hand, never on a push to main.

GitHub at times has no macOS arm64 runner for a push and cancels the job after
900 s (#1003). The workflow files are read, not run.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
STEPS = [
    "Checkout repository",
    "Set up Python",
    "Build wheel",
    "Install wheel",
    "Import check",
]


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _platforms(job: dict) -> list[dict]:
    return job["strategy"]["matrix"]["platform"]


def test_only_macos_jobs_are_skipped_on_push():
    jobs = _load("packaging-smoke.yml")["jobs"].values()
    for job in jobs:
        on_macos = [p for p in _platforms(job) if p["runner"].startswith("macos")]
        if on_macos:
            assert job.get("if") == "github.event_name != 'push'"
            assert len(_platforms(job)) == 1
        else:
            assert "if" not in job


def test_every_platform_is_covered_with_the_same_steps():
    jobs = _load("packaging-smoke.yml")["jobs"].values()
    names = sorted(p["name"] for job in jobs for p in _platforms(job))
    assert names == ["linux", "macos", "windows"]
    for job in jobs:
        assert [step["name"] for step in job["steps"]] == STEPS
    # The macos job repeats the steps, so a drifted body must fail here.
    first, *rest = (job["steps"] for job in jobs)
    assert all(steps == first for steps in rest)


def test_a_release_calls_the_smoke_with_an_event_that_is_never_push():
    release = _load("release.yml")
    # PyYAML reads the key `on` as the boolean True.
    assert list(release[True]) == ["workflow_dispatch"]
    assert (
        release["jobs"]["packaging-smoke"]["uses"]
        == "./.github/workflows/packaging-smoke.yml"
    )
