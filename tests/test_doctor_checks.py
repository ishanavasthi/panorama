"""Unit tests for the `panorama.doctor` check registry itself.

Complements `tests/test_doctor.py`, which drives `doctor` through the CLI and
asserts on rendered text. This file tests the pure logic underneath: individual
check outcomes, aggregation, exit-code mapping, and the deep boundary probe.

Nothing here shells out to `claude`, and nothing here needs a subscription: the
deep check is exercised through an injected boundary probe.
"""

from __future__ import annotations

import inspect
import json
import stat
import sys
from pathlib import Path

import pytest

from panorama import doctor
from panorama.doctor import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    BoundaryProbeResult,
    CheckContext,
    CheckResult,
    CheckSpec,
    DoctorReport,
    render_report,
    run_doctor,
)
from panorama.errors import (
    EXIT_CLAUDE_INVOCATION_ERROR,
    EXIT_OK,
    EXIT_PREFLIGHT_ERROR,
    ClaudeInvocationError,
    PreflightError,
)

# --- CheckResult invariants ------------------------------------------------


def test_failure_without_remediation_is_rejected() -> None:
    """Every failure must be actionable; the type enforces it."""
    with pytest.raises(ValueError, match="without a remediation"):
        CheckResult("x", STATUS_FAIL, "broken")


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown status"):
        CheckResult("x", "exploded", "broken")


# --- Aggregation and exit codes -------------------------------------------


def _report(*results: CheckResult, deep: bool = False) -> DoctorReport:
    return DoctorReport(results=results, deep=deep)


def test_empty_report_is_ok() -> None:
    assert _report().status == STATUS_OK
    assert _report().exit_code == EXIT_OK


def test_warnings_do_not_fail_the_report() -> None:
    report = _report(
        CheckResult("a", STATUS_OK, "fine"),
        CheckResult("b", STATUS_WARN, "meh", remediation="ignore me"),
    )
    assert report.status == STATUS_WARN
    assert report.ok is True
    assert report.exit_code == EXIT_OK


def test_failure_maps_to_preflight_exit_code() -> None:
    report = _report(
        CheckResult("a", STATUS_WARN, "meh", remediation="x"),
        CheckResult("b", STATUS_FAIL, "broken", remediation="fix it"),
    )
    assert report.status == STATUS_FAIL
    assert report.ok is False
    assert report.exit_code == EXIT_PREFLIGHT_ERROR


def test_boundary_failure_maps_to_claude_invocation_exit_code() -> None:
    report = _report(
        CheckResult(
            "claude-boundary",
            STATUS_FAIL,
            "round trip failed",
            remediation="fix it",
            error_type=ClaudeInvocationError,
        )
    )
    assert report.exit_code == EXIT_CLAUDE_INVOCATION_ERROR


def test_first_failure_wins_the_exit_code() -> None:
    """Checks run environment-first, so the most upstream failure is reported."""
    report = _report(
        CheckResult("git", STATUS_FAIL, "missing", remediation="install git"),
        CheckResult(
            "claude-boundary",
            STATUS_FAIL,
            "round trip failed",
            remediation="fix it",
            error_type=ClaudeInvocationError,
        ),
    )
    assert report.exit_code == EXIT_PREFLIGHT_ERROR


def test_raise_for_status() -> None:
    _report(CheckResult("a", STATUS_OK, "fine")).raise_for_status()  # no raise
    with pytest.raises(PreflightError):
        _report(CheckResult("a", STATUS_FAIL, "broken", remediation="fix")).raise_for_status()


# --- Individual checks -----------------------------------------------------


def test_python_check_passes_on_this_interpreter() -> None:
    assert doctor.check_python(CheckContext()).status == STATUS_OK


def test_missing_binary_fails_with_remediation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    for check in (
        doctor.check_git,
        doctor.check_gh,
        doctor.check_claude,
        doctor.check_gh_auth,
        doctor.check_claude_auth,
    ):
        result = check(CheckContext())
        assert result.status == STATUS_FAIL, check.__name__
        assert result.remediation, check.__name__


def test_git_version_below_minimum_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(
        doctor, "_run", lambda *a, **k: doctor._Completed(True, 0, "git version 1.9.0")
    )
    result = doctor.check_git(CheckContext())
    assert result.status == STATUS_FAIL
    assert "Upgrade git" in (result.remediation or "")


def test_old_claude_warns_rather_than_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unverified CLI build is a caveat, not a hard stop: `--deep` is what
    actually proves the boundary on that build."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        doctor, "_run", lambda *a, **k: doctor._Completed(True, 0, "1.0.1 (Claude Code)")
    )
    result = doctor.check_claude(CheckContext())
    assert result.status == STATUS_WARN
    assert "--deep" in (result.remediation or "")


def test_gh_auth_parses_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/gh")
    payload = (
        '{"hosts":{"github.com":[{"state":"success","active":true,'
        '"login":"octocat","tokenSource":"keyring"}]}}'
    )
    monkeypatch.setattr(doctor, "_run", lambda *a, **k: doctor._Completed(True, 0, payload))

    result = doctor.check_gh_auth(CheckContext())
    assert result.status == STATUS_OK
    assert "octocat" in result.detail
    assert "github.com" in result.detail


def test_gh_auth_never_leaks_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when gh hands us a token, it must not reach the rendered result."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/gh")
    leaky = (
        "github.com\n"
        "  Logged in to github.com account octocat (keyring)\n"
        "  - Token: gho_SUPERSECRETVALUE\n"
        "  - Token scopes: 'repo'\n"
    )
    monkeypatch.setattr(doctor, "_run", lambda *a, **k: doctor._Completed(True, 0, leaky))

    rendered = render_report(_report(doctor.check_gh_auth(CheckContext())))
    assert "gho_" not in rendered
    assert "SUPERSECRET" not in rendered
    assert "octocat" in rendered


def test_gh_auth_never_requests_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--show-token` must never be passed."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/gh")
    invoked: list[list[str]] = []

    def fake_run(argv, *, timeout=0.0, env=None):
        invoked.append(list(argv))
        return doctor._Completed(False, 1, "")

    monkeypatch.setattr(doctor, "_run", fake_run)
    doctor.check_gh_auth(CheckContext())

    for argv in invoked:
        assert "--show-token" not in argv
        assert "-t" not in argv


def test_gh_auth_unauthenticated_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(doctor, "_run", lambda *a, **k: doctor._Completed(False, 1, ""))

    result = doctor.check_gh_auth(CheckContext())
    assert result.status == STATUS_FAIL
    assert "gh auth login" in (result.remediation or "")


def _auth_payload(**overrides: object) -> str:
    base: dict[str, object] = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "person@example.com",
        "orgId": "0000-org-id",
        "orgName": "Someone's Organization",
        "subscriptionType": "max",
    }
    base.update(overrides)
    return json.dumps(base)


def test_claude_auth_subscription_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(doctor, "_run", lambda *a, **k: doctor._Completed(True, 0, _auth_payload()))

    result = doctor.check_claude_auth(CheckContext())
    assert result.status == STATUS_OK
    assert "subscription" in result.detail


def test_claude_auth_never_leaks_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """email / orgId / orgName come back from the CLI and must be dropped."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(doctor, "_run", lambda *a, **k: doctor._Completed(True, 0, _auth_payload()))

    rendered = render_report(_report(doctor.check_claude_auth(CheckContext())))
    assert "person@example.com" not in rendered
    assert "0000-org-id" not in rendered
    assert "Organization" not in rendered


def test_claude_auth_logged_out_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(
        doctor,
        "_run",
        lambda *a, **k: doctor._Completed(True, 0, _auth_payload(loggedIn=False)),
    )

    result = doctor.check_claude_auth(CheckContext())
    assert result.status == STATUS_FAIL
    assert "/login" in (result.remediation or "")


def test_claude_auth_non_subscription_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An API-key-authenticated CLI is not an acceptable substitute."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(
        doctor,
        "_run",
        lambda *a, **k: doctor._Completed(True, 0, _auth_payload(authMethod="apiKey")),
    )

    result = doctor.check_claude_auth(CheckContext())
    assert result.status == STATUS_FAIL
    assert "subscription" in result.detail


def test_claude_auth_runs_under_the_minimal_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth must be proven in the same environment the real child gets."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/claude")
    seen: dict[str, object] = {}

    def fake_run(argv, *, timeout=0.0, env=None):
        seen["argv"] = list(argv)
        seen["env"] = env
        return doctor._Completed(True, 0, _auth_payload())

    monkeypatch.setattr(doctor, "_run", fake_run)
    doctor.check_claude_auth(CheckContext())

    assert seen["argv"][1:] == ["auth", "status", "--json"]  # type: ignore[index]
    assert set(seen["env"]) == {"PATH", "HOME", "USER"}  # type: ignore[arg-type]


def test_doctor_never_mutates_auth_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """No check may ever invoke a state-changing auth subcommand."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    invoked: list[list[str]] = []

    def fake_run(argv, *, timeout=0.0, env=None):
        invoked.append(list(argv))
        return doctor._Completed(False, 1, "")

    monkeypatch.setattr(doctor, "_run", fake_run)
    run_doctor(deep=False, context=CheckContext(create_workspace=False))

    forbidden = {"login", "logout", "setup-token", "switch", "refresh", "token"}
    for argv in invoked:
        assert not forbidden.intersection(argv[1:]), argv


def test_anthropic_api_key_warns_and_never_reads_the_value() -> None:
    ctx = CheckContext(environ={"ANTHROPIC_API_KEY": "sk-ant-DO-NOT-LEAK"})
    result = doctor.check_anthropic_api_key_unused(ctx)

    assert result.status == STATUS_WARN
    assert "ANTHROPIC_API_KEY" in result.detail
    assert "does not use it" in result.detail
    rendered = render_report(_report(result))
    assert "sk-ant" not in rendered
    assert "DO-NOT-LEAK" not in rendered


def test_anthropic_api_key_warning_is_not_a_failure() -> None:
    """Panorama ignoring a key is informational; it must not block a review."""
    ctx = CheckContext(environ={"ANTHROPIC_API_KEY": "sk-ant-x"})
    report = _report(doctor.check_anthropic_api_key_unused(ctx))
    assert report.exit_code == EXIT_OK


def test_anthropic_api_key_absent_is_ok() -> None:
    result = doctor.check_anthropic_api_key_unused(CheckContext(environ={}))
    assert result.status == STATUS_OK


# --- Workspace -------------------------------------------------------------


def test_workspace_created_with_0700(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    result = doctor.check_workspace(CheckContext(workspace_root=root))

    assert result.status == STATUS_OK
    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_workspace_with_loose_permissions_fails(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    root.chmod(0o755)

    result = doctor.check_workspace(CheckContext(workspace_root=root))
    assert result.status == STATUS_FAIL
    assert "0755" in result.detail
    assert "chmod 700" in (result.remediation or "")


def test_workspace_that_is_a_file_fails(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.write_text("not a directory", encoding="utf-8")

    result = doctor.check_workspace(CheckContext(workspace_root=root))
    assert result.status == STATUS_FAIL
    assert result.remediation


def test_workspace_not_created_when_disabled(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    result = doctor.check_workspace(CheckContext(workspace_root=root, create_workspace=False))
    assert result.status == STATUS_WARN
    assert not root.exists()


# --- Deep check ------------------------------------------------------------


def test_deep_check_is_skipped_by_default() -> None:
    report = run_doctor(deep=False, context=CheckContext(create_workspace=False))
    boundary = next(r for r in report.results if r.name == "claude-boundary")
    assert boundary.status == STATUS_SKIP
    assert "--deep" in boundary.detail


def test_deep_check_passes_when_the_marker_round_trips() -> None:
    captured: dict[str, object] = {}

    def probe(*, workspace: Path, schema, prompt: str, timeout: float):
        captured["timeout"] = timeout
        captured["schema"] = schema
        marker = (workspace / "marker.txt").read_text(encoding="utf-8").strip()
        return BoundaryProbeResult(ok=True, structured_output={"marker": marker})

    ctx = CheckContext(deep=True, boundary_probe=probe, deep_timeout_s=12.0)
    result = doctor.check_claude_boundary(ctx)

    assert result.status == STATUS_OK
    assert captured["timeout"] == 12.0  # the hard bound is passed through
    assert captured["schema"]["required"] == ["marker"]  # type: ignore[index]


def test_deep_check_fails_when_the_marker_is_wrong() -> None:
    """Schema-conforming output is not enough; the file must really be read."""

    def probe(*, workspace: Path, schema, prompt: str, timeout: float):
        return BoundaryProbeResult(ok=True, structured_output={"marker": "guessed"})

    result = doctor.check_claude_boundary(CheckContext(deep=True, boundary_probe=probe))
    assert result.status == STATUS_FAIL
    assert result.error_type is ClaudeInvocationError


def test_deep_check_surfaces_probe_failure_remediation() -> None:
    def probe(*, workspace: Path, schema, prompt: str, timeout: float):
        return BoundaryProbeResult(ok=False, failure="timed out", remediation="raise the timeout")

    result = doctor.check_claude_boundary(CheckContext(deep=True, boundary_probe=probe))
    assert result.status == STATUS_FAIL
    assert result.remediation == "raise the timeout"
    assert "timed out" in result.detail


def test_deep_check_probe_workspace_is_isolated_and_removed() -> None:
    seen: list[Path] = []

    def probe(*, workspace: Path, schema, prompt: str, timeout: float):
        seen.append(workspace)
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        assert [p.name for p in workspace.iterdir()] == ["marker.txt"]
        marker = (workspace / "marker.txt").read_text(encoding="utf-8").strip()
        return BoundaryProbeResult(ok=True, structured_output={"marker": marker})

    doctor.check_claude_boundary(CheckContext(deep=True, boundary_probe=probe))
    assert not seen[0].exists()


def test_deep_check_marker_is_not_hard_coded() -> None:
    """Two runs must use different tokens, or the probe proves nothing."""
    markers: list[str] = []

    def probe(*, workspace: Path, schema, prompt: str, timeout: float):
        markers.append((workspace / "marker.txt").read_text(encoding="utf-8").strip())
        return BoundaryProbeResult(ok=True, structured_output={"marker": markers[-1]})

    for _ in range(2):
        doctor.check_claude_boundary(CheckContext(deep=True, boundary_probe=probe))
    assert markers[0] != markers[1]


def test_default_probe_degrades_when_runner_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing transport module must be reported, not raised."""
    monkeypatch.setitem(sys.modules, "panorama.claude_runner", None)
    result = doctor._default_boundary_probe(
        workspace=tmp_path, schema=doctor.PROBE_SCHEMA, prompt="x", timeout=1.0
    )
    assert result.ok is False
    assert "ClaudeRunner" in (result.failure or "")
    assert result.remediation


class _FakeRunner:
    """Stands in for ClaudeRunner so the doctor->runner contract is testable
    offline. If either signature drifts, the test below fails instead of
    `--deep` failing in front of an operator."""

    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs

    def run(self, **kwargs: object) -> object:
        _FakeRunner.calls.append({**kwargs, "_init": self.init_kwargs})
        marker = (Path(str(kwargs["workspace"])) / "marker.txt").read_text(encoding="utf-8").strip()

        class _Result:
            data = {"marker": marker}

        return _Result()


def test_default_probe_drives_the_real_runner_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe must call ClaudeRunner exactly as ClaudeRunner is written."""
    from panorama import claude_runner

    # Capture the real signatures before the class is swapped out.
    run_params = set(inspect.signature(claude_runner.ClaudeRunner.run).parameters)
    init_params = set(inspect.signature(claude_runner.ClaudeRunner.__init__).parameters)

    _FakeRunner.calls = []
    monkeypatch.setattr(claude_runner, "ClaudeRunner", _FakeRunner)
    (tmp_path / "marker.txt").write_text("TOKEN123\n", encoding="utf-8")

    result = doctor._default_boundary_probe(
        workspace=tmp_path, schema=doctor.PROBE_SCHEMA, prompt="find it", timeout=7.0
    )
    assert result.ok is True
    assert result.structured_output == {"marker": "TOKEN123"}

    (call,) = _FakeRunner.calls
    init_kwargs = call.pop("_init")
    assert init_kwargs["timeout_seconds"] == 7.0
    # The probe workspace is a temp tree, not a real clone under
    # ~/.panorama/workspaces, so the probe must widen `--add-dir` containment
    # explicitly -- and widen it to exactly that directory, nothing broader.
    assert init_kwargs["allowed_workspace_roots"] == [tmp_path]
    # Run artifacts must not land inside the probe workspace.
    assert tmp_path not in Path(str(call["artifact_dir"])).parents

    # Every kwarg the probe sends must exist on the real ClaudeRunner.
    assert set(call) <= run_params
    assert set(init_kwargs) <= init_params  # type: ignore[arg-type]


def test_default_probe_surfaces_runner_error_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ClaudeInvocationError messages are host-authored and already redacted,
    so they are safe to pass through verbatim."""
    from panorama import claude_runner

    class _Failing:
        def __init__(self, **kwargs: object) -> None: ...

        def run(self, **kwargs: object) -> object:
            raise ClaudeInvocationError("host-authored redacted message")

    monkeypatch.setattr(claude_runner, "ClaudeRunner", _Failing)
    result = doctor._default_boundary_probe(
        workspace=tmp_path, schema=doctor.PROBE_SCHEMA, prompt="x", timeout=1.0
    )
    assert result.ok is False
    assert result.failure == "host-authored redacted message"


def test_default_probe_hides_unexpected_exception_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An arbitrary exception's message may embed paths or child output."""
    from panorama import claude_runner

    class _Exploding:
        def __init__(self, **kwargs: object) -> None: ...

        def run(self, **kwargs: object) -> object:
            raise RuntimeError("/absolute/operator/path/leaked")

    monkeypatch.setattr(claude_runner, "ClaudeRunner", _Exploding)
    result = doctor._default_boundary_probe(
        workspace=tmp_path, schema=doctor.PROBE_SCHEMA, prompt="x", timeout=1.0
    )
    assert result.ok is False
    assert "leaked" not in (result.failure or "")
    assert "RuntimeError" in (result.failure or "")


def test_minimal_child_env_matches_the_runner_env() -> None:
    """doctor must probe auth under the same environment reviews run under."""
    from panorama import claude_runner

    assert doctor.minimal_child_env() == claude_runner._child_env()


# --- Driver and renderer ---------------------------------------------------


def test_a_raising_check_becomes_a_failure_not_a_crash() -> None:
    def boom(ctx: CheckContext) -> CheckResult:
        raise RuntimeError("kaboom")

    report = run_doctor(checks=[CheckSpec("explodes", boom)])
    assert report.results[0].status == STATUS_FAIL
    assert "RuntimeError" in report.results[0].detail
    assert "kaboom" not in report.results[0].detail  # message not surfaced
    assert report.exit_code == EXIT_PREFLIGHT_ERROR


def test_registry_order_is_run_order() -> None:
    """Environment first, Claude boundary last -- the order the exit-code rule
    depends on."""
    names = [spec.name for spec in doctor.CHECKS]
    assert names == [
        "python",
        "git",
        "gh",
        "gh-auth",
        "claude",
        "claude-auth",
        "anthropic-api-key",
        "workspace",
        "claude-boundary",
    ]


def test_only_the_boundary_check_is_deep_only() -> None:
    deep_only = [spec.name for spec in doctor.CHECKS if spec.deep_only]
    assert deep_only == ["claude-boundary"]


def test_render_report_includes_remediation_for_failures() -> None:
    report = _report(
        CheckResult("git", STATUS_OK, "git 2.50.1."),
        CheckResult("gh-auth", STATUS_FAIL, "not authenticated", "run `gh auth login`"),
    )
    text = render_report(report)

    assert "[ ok ]" in text
    assert "[FAIL]" in text
    assert "run `gh auth login`" in text
    assert "1 failed" in text
    assert "Preflight did not pass" in text


def test_render_report_suggests_deep_when_shallow_and_clean() -> None:
    text = render_report(_report(CheckResult("git", STATUS_OK, "git 2.50.1.")))
    assert "--deep" in text


def test_render_report_confirms_deep_run() -> None:
    text = render_report(_report(CheckResult("git", STATUS_OK, "git 2.50.1."), deep=True))
    assert "including the live Claude CLI round trip" in text


def test_report_to_dict_is_json_serialisable() -> None:
    report = _report(
        CheckResult("git", STATUS_OK, "git 2.50.1."),
        CheckResult("gh-auth", STATUS_FAIL, "nope", "run `gh auth login`"),
    )
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["status"] == STATUS_FAIL
    assert payload["exit_code"] == EXIT_PREFLIGHT_ERROR
    assert [c["name"] for c in payload["checks"]] == ["git", "gh-auth"]
