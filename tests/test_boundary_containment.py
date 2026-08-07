"""Regression tests for the boundary defects found in the M0 adversarial audit.

Each test here exists because a real defect shipped past a green suite. The
suite was not merely missing coverage -- in two cases it asserted the right
property against a code path that could not exercise it, so it reported green
while the leak was live. The comments record which blind spot each test closes.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from panorama import config
from panorama.claude_runner import ClaudeRunner, safe_token
from panorama.errors import ClaudeInvocationError

# --- `--add-dir` containment -------------------------------------------------
#
# Blind spot: `--add-dir` was interpolated straight from the caller with no
# validation, and `grep -rn 'add.dir' tests/` matched nothing but an alias
# table. Restricting the tool set to Read/Grep/Glob bounds what the model can
# DO; this bounds what it can do it TO.


@pytest.fixture
def contained_runner(tmp_path: Path) -> tuple[ClaudeRunner, Path]:
    """A runner whose only allowed root is a temp workspace."""
    root = tmp_path / "workspaces"
    (root / "acme-api").mkdir(parents=True)
    return ClaudeRunner(allowed_workspace_roots=[root]), root


def test_workspace_inside_an_allowed_root_is_accepted(contained_runner):
    runner, root = contained_runner
    argv = runner.build_argv(json_schema={"type": "object"}, workspace=root)
    assert argv[argv.index("--add-dir") + 1] == str(root.resolve())


def test_repo_inside_the_allowed_root_is_accepted(contained_runner):
    """Containment permits descendants, not just the root itself."""
    runner, root = contained_runner
    argv = runner.build_argv(json_schema={"type": "object"}, workspace=root / "acme-api")
    assert argv[argv.index("--add-dir") + 1] == str((root / "acme-api").resolve())


@pytest.mark.parametrize("escape", ["/", "~", ".."])
def test_workspace_outside_the_allowed_root_is_refused(contained_runner, escape):
    """`--add-dir /` or `~` would reach ~/.ssh and ~/.config/gh/hosts.yml."""
    runner, root = contained_runner
    target = root / escape if escape == ".." else Path(escape)
    with pytest.raises(ClaudeInvocationError, match="outside the Panorama workspace"):
        runner.build_argv(json_schema={"type": "object"}, workspace=target)


def test_symlink_out_of_the_workspace_cannot_smuggle_access(contained_runner, tmp_path):
    """Containment resolves before comparing, so a symlink is not an escape."""
    runner, root = contained_runner
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "escape-hatch").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ClaudeInvocationError, match="outside the Panorama workspace"):
        runner.build_argv(json_schema={"type": "object"}, workspace=root / "escape-hatch")


def test_nonexistent_workspace_is_refused(contained_runner):
    runner, root = contained_runner
    with pytest.raises(ClaudeInvocationError, match="not an existing directory"):
        runner.build_argv(json_schema={"type": "object"}, workspace=root / "never-cloned")


def test_default_allowed_root_is_the_production_workspace(tmp_path):
    """A runner built with no explicit allowlist trusts only ~/.panorama/workspaces."""
    assert ClaudeRunner().allowed_workspace_roots == (config.WORKSPACE_ROOT.resolve(),)


# --- directory permissions ---------------------------------------------------
#
# Blind spot: `Path.mkdir(parents=True, mode=0700)` applies the mode to the LEAF
# ONLY. The doctor check inspected just the leaf, so it reported ok against
# 0755 parents.


def test_ensure_dir_locks_down_every_level_not_just_the_leaf(tmp_path):
    leaf = config.ensure_dir(tmp_path / "a" / "b" / "c")

    for level in (tmp_path / "a", tmp_path / "a" / "b", leaf):
        mode = stat.S_IMODE(level.stat().st_mode)
        assert mode == stat.S_IRWXU, f"{level} is {mode:04o}, expected 0700"


def test_ensure_dir_leaves_an_existing_directory_alone(tmp_path):
    existing = tmp_path / "already-here"
    existing.mkdir(mode=0o755)
    os.chmod(existing, 0o755)

    config.ensure_dir(existing)

    assert stat.S_IMODE(existing.stat().st_mode) == 0o755


def test_run_artifact_directory_is_created_owner_only(tmp_path):
    runner = ClaudeRunner(artifact_root=tmp_path / "runs")
    created = runner._prepare_artifact_dir(None)
    assert stat.S_IMODE(created.stat().st_mode) == stat.S_IRWXU


def test_run_artifact_directory_refuses_to_fail_open(tmp_path, monkeypatch):
    """The artifact dir receives verbatim private-repo source.

    If the chmod does not take effect, the old code swallowed the error and
    handed raw child output to a directory of unknown mode. It must raise.
    """
    runner = ClaudeRunner(artifact_root=tmp_path / "runs")
    loosened = tmp_path / "runs" / "loosened"
    loosened.mkdir(parents=True)
    loosened.chmod(0o755)

    # Neutralise the chmod so the post-condition check is what decides.
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)

    with pytest.raises(ClaudeInvocationError, match="expected 0700"):
        runner._prepare_artifact_dir(loosened)


# --- child-derived string rendering -----------------------------------------
#
# Blind spot: the existing test proved the `Token:` LINE of `gh auth status` was
# not echoed, but the login field was captured with an unbounded `(\S+)` and
# rendered raw.


@pytest.mark.parametrize(
    "hostile",
    [
        "gho_SECRET_LEAKED_VIA_LOGIN_FIELD_0123456789012345678901234567890",
        "name with spaces",
        "line\nbreak",
        "semi;colon",
        "$(whoami)",
    ],
)
def test_safe_token_rejects_hostile_child_strings(hostile):
    assert safe_token(hostile) is None


@pytest.mark.parametrize("benign", ["ishanavasthi", "github.com", "some-user_1.2"])
def test_safe_token_passes_ordinary_account_names(benign):
    assert safe_token(benign) == benign
