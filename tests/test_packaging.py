"""Deployment artifacts: the Dockerfile, the CI pipeline, the dependency sets.

These are the parts nobody runs locally, so they rot silently. Each assertion
here pins a property that would be expensive to discover in production: a
pipeline that recurses, an image that runs as root, a dev tool that leaked into
the runtime dependency set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
CI = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
RUNTIME_REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
DEV_REQUIREMENTS = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- Dockerfile


def test_base_image_is_pinned_by_digest() -> None:
    """An image rebuilt in six months must behave identically (§18.3)."""
    froms = re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.MULTILINE)
    assert froms, "no FROM instruction found"
    for image in froms:
        assert "@sha256:" in image, f"{image} is pinned by tag, not digest"


def test_both_stages_pin_the_same_digest() -> None:
    """A builder and runtime that drift apart defeat the point of pinning."""
    digests = set(re.findall(r"@(sha256:[0-9a-f]{64})", DOCKERFILE))
    assert len(digests) == 1


def test_image_runs_as_a_non_root_user() -> None:
    """Least privilege in the container (§18.2)."""
    assert re.search(r"^USER\s+agent", DOCKERFILE, re.MULTILINE)
    assert "useradd" in DOCKERFILE


def test_image_pins_locale_and_timezone() -> None:
    """Container locale must never alter normalized output (§18.3)."""
    for setting in ("LANG=C.UTF-8", "LC_ALL=C.UTF-8", "TZ=UTC"):
        assert setting in DOCKERFILE


def test_image_installs_git() -> None:
    """The agent drives the `git` CLI (decision D-4)."""
    assert "git" in DOCKERFILE.split("apt-get install")[1].split("\n")[0]


def test_image_carries_no_secret_defaults() -> None:
    """No credential may be baked into a layer via ARG or ENV (§18.2)."""
    for line in DOCKERFILE.splitlines():
        if line.strip().startswith(("ARG", "ENV")):
            assert not re.search(r"(password|token|secret)\s*=", line, re.IGNORECASE)


def test_only_runtime_dependencies_reach_the_image() -> None:
    """pytest, ruff, and mypy must never be installed into the runtime image."""
    assert "requirements-dev" not in DOCKERFILE
    assert "COPY requirements.txt" in DOCKERFILE


def test_dockerignore_excludes_tests_and_history() -> None:
    """Keeps the build context small and the image free of the repository."""
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for entry in (".git/", "tests/", "docs/", ".venv/"):
        assert entry in ignored


# --------------------------------------------------------------------------- dependencies


def test_runtime_dependency_set_is_the_approved_one() -> None:
    """CLAUDE.md §4: oracledb and PyYAML, nothing else without approval."""
    pinned = re.findall(r"^([A-Za-z0-9_.-]+)==", RUNTIME_REQUIREMENTS, re.MULTILINE)
    assert sorted(pinned) == ["PyYAML", "oracledb"]


def test_every_runtime_dependency_is_fully_pinned() -> None:
    """A floating version makes an image rebuild non-deterministic (§18.3)."""
    for line in RUNTIME_REQUIREMENTS.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            assert "==" in stripped, f"{stripped!r} is not pinned"


def test_dev_dependencies_are_pinned_and_include_the_ci_toolchain() -> None:
    """Lint, type check, and tests are blocking in CI, so CI must have them."""
    pinned = dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", DEV_REQUIREMENTS, re.MULTILINE))
    assert {"pytest", "ruff", "mypy"} <= set(pinned)


def test_dev_requirements_include_the_runtime_set() -> None:
    """One source of truth for the runtime pins."""
    assert "-r requirements.txt" in DEV_REQUIREMENTS


# --------------------------------------------------------------------------- CI pipeline


def test_pipeline_declares_the_specified_stages() -> None:
    """docs/architecture.md §20.1."""
    assert CI["stages"] == ["validate", "build", "sync"]


@pytest.mark.parametrize("job", ["unit-tests", "lint", "type-check", "build-image", "sync"])
def test_required_jobs_exist(job: str) -> None:
    """Each stage's job from §20.1 is present."""
    assert job in CI


@pytest.mark.parametrize("job", ["unit-tests", "lint", "type-check"])
def test_validate_jobs_are_blocking(job: str) -> None:
    """CLAUDE.md §6: these must fail the pipeline, not warn.

    A job that is `allow_failure` or `manual` is not a gate.
    """
    definition = CI[job]
    assert definition.get("allow_failure") is not True
    for rule in definition.get("rules", []):
        assert rule.get("when") != "manual"


def test_sync_cannot_be_triggered_by_a_push() -> None:
    """The agent pushes to the repository that triggers it (§11.7).

    Guard one of two: `rules` admits only `schedule` and `web`.
    """
    sources = {
        rule["if"] for rule in CI["sync"]["rules"] if "if" in rule
    }
    assert sources == {
        '$CI_PIPELINE_SOURCE == "schedule"',
        '$CI_PIPELINE_SOURCE == "web"',
    }


def test_commit_messages_carry_the_skip_ci_marker() -> None:
    """Guard two of two, independent of the `rules` clause (§11.7)."""
    from src.git.commit import SKIP_CI_MARKER

    assert SKIP_CI_MARKER == "[skip ci]"


def test_sync_is_serialized() -> None:
    """Two scheduled runs must not race on the same branch (§11.8).

    This is also what makes the amend + force-with-lease strategy safe (D-3).
    """
    assert CI["sync"]["resource_group"] == "oracle-versioning-sync"
    assert CI["sync"]["interruptible"] is False


def test_sync_fetches_full_history() -> None:
    """Rebase and push need real history, not a shallow snapshot (§19)."""
    assert CI["sync"]["variables"]["GIT_DEPTH"] == "0"


def test_sync_has_a_timeout() -> None:
    """No execution may hang until the runner's own timeout (§19)."""
    assert "timeout" in CI["sync"]


def test_secret_scanning_runs_in_validate() -> None:
    """docs/architecture.md §21.5."""
    includes = str(CI.get("include", ""))
    assert "Secret-Detection" in includes


def test_no_credentials_are_hardcoded_in_the_pipeline() -> None:
    """Every secret comes from a masked CI/CD variable (§20, §21.1)."""
    raw = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    for match in re.findall(r"(?i)(password|token|secret)\s*[:=]\s*(\S+)", raw):
        assert match[1].startswith(("$", '"$', "'$")), f"literal {match[0]} in the pipeline"


# --------------------------------------------------------------------------- documentation


def test_runbook_covers_every_failure_mode() -> None:
    """An operator woken at 2am needs the recovery, not the design."""
    runbook = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    for symptom in (
        "cannot connect",            # database unreachable
        "cannot read",               # missing grants
        "not valid JSON",            # corrupt state
        "Authentication failed",     # push credential
        "reconciliation BLOCKED",    # mass-deletion guard
        "no effective change",       # the quiet, correct outcome
        "wall-clock timeout",        # run-unit budget
        "--allow-mass-delete",       # the override, with a warning attached
    ):
        assert symptom in runbook, f"runbook does not cover {symptom!r}"


def test_traceability_matrix_records_every_deviation() -> None:
    """Deviations are on the record, not buried in a commit message."""
    matrix = (ROOT / "docs" / "traceability.md").read_text(encoding="utf-8")
    for deviation in [f"**D-{n}**" for n in range(7, 15)]:
        assert deviation in matrix, f"{deviation} is not documented"


def test_traceability_matrix_covers_the_approved_decisions() -> None:
    """D-1 to D-6 are binding and must each map to code and a test."""
    matrix = (ROOT / "docs" / "traceability.md").read_text(encoding="utf-8")
    for decision in ("D-1", "D-2", "D-3", "D-4", "D-5", "D-6"):
        assert f"| {decision} |" in matrix


def test_traceability_matrix_covers_every_behavioural_scenario() -> None:
    """Each scenario in architecture §22 maps to a test."""
    matrix = (ROOT / "docs" / "traceability.md").read_text(encoding="utf-8")
    for scenario in ("§22.1", "§22.2", "§22.3a", "§22.3b", "§22.4", "§22.5", "§22.6", "§22.7"):
        assert scenario in matrix


def test_deployment_guide_exists_and_covers_the_essentials() -> None:
    """A pipeline nobody can set up is not deployed."""
    guide = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for topic in (
        "SELECT_CATALOG_ROLE",   # the read-only grant
        "GIT_PUSH_TOKEN",        # the push credential
        "write_repository",      # its scope
        "Schedules",             # how executions are triggered
        "--dry-run",             # how to validate before scheduling
        "resource_group" if "resource_group" in guide else "tags",
    ):
        assert topic in guide, f"deployment guide does not mention {topic}"
