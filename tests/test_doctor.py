"""Offline tests for `panorama doctor`.

Driven primarily through ``run_doctor`` / ``render_report``, because that pair
is the real contract: `doctor` exists to tell a human what to fix, and it must
do so without ever echoing a token, an email, an org id, or raw tool output.

``CheckContext`` makes this cleanly testable offline -- it takes an explicit
``environ`` and ``workspace_root`` -- so no test here mutates global state
beyond ``PATH``, and none reaches a real tool.

The environment is faked two ways:

* **binary missing** -- ``PATH`` points at an empty directory, so every
  binary check must fail and must carry a remediation;
* **binaries present** -- ``PATH`` points at the fake bin dir, whose ``claude``
  and ``gh`` deliberately emit secret-shaped values that must not survive
  into rendered output.

A thin CLI-level suite is included too, skipped while `panorama doctor` is
still the scaffold stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SECRET_SENTINELS, assert_no_secrets

REQUIRED_TOOLS = ("claude", "git", "gh")

#: A rendered failure has to tell the operator what to do about it.
REMEDIATION_HINTS = (
    "install",
    "run ",
    "see ",
    "http",
    "brew",
    "login",
    "not found",
    "missing",
    "add ",
    "path",
)

ENV_SENTINEL = "PANORAMA-ENV-SECRET-VALUE"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def empty_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH containing no executables at all."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return empty


@pytest.fixture
def partial_path(tmp_path: Path, fake_bin, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH where `claude` is present but `gh` and `git` are absent."""
    partial = tmp_path / "partial-bin"
    partial.mkdir()
    (partial / "claude").symlink_to(fake_bin.path)
    monkeypatch.setenv("PATH", str(partial))
    return partial


@pytest.fixture
def fake_path(fake_bin, monkeypatch: pytest.MonkeyPatch):
    """A PATH where all three fakes are present and emitting secrets."""
    monkeypatch.setenv("PATH", str(fake_bin.bin_dir))
    fake_bin.set_scenario({"canary": fake_bin.canary})
    return fake_bin


@pytest.fixture
def make_context(doctor_module: Any, tmp_path: Path):
    """Build a ``CheckContext`` pinned to a throwaway workspace root.

    Without the override, checks would probe (and create) the operator's real
    ``~/.panorama/workspaces``.
    """

    def factory(**overrides: Any):
        values: dict[str, Any] = {
            "workspace_root": tmp_path / "workspaces",
            "environ": {},
        }
        values.update(overrides)
        return doctor_module.CheckContext(**values)

    return factory


def rendered(doctor_module: Any, report: Any) -> str:
    return doctor_module.render_report(report)


# --------------------------------------------------------------------------
# missing binaries -> failure with remediation
# --------------------------------------------------------------------------


def test_report_fails_when_every_binary_is_missing(doctor_module, make_context, empty_path):
    report = doctor_module.run_doctor(context=make_context())

    assert not report.ok, "doctor reported success with an empty PATH"
    assert report.status == doctor_module.STATUS_FAIL
    assert report.failures, "no individual check was marked as failed"


def test_each_missing_binary_produces_a_failing_check(
    doctor_module, make_context, empty_path
):
    report = doctor_module.run_doctor(context=make_context())
    failed_names = " ".join(r.name for r in report.failures).lower()

    for tool in REQUIRED_TOOLS:
        assert tool in failed_names, f"no failing check named for the missing {tool!r}"


def test_every_failing_check_carries_a_remediation(doctor_module, make_context, empty_path):
    """A failure with no next step is a worse experience than a stack trace."""
    report = doctor_module.run_doctor(context=make_context())

    for result in report.failures:
        assert result.remediation, f"check {result.name!r} failed with no remediation"
        assert result.remediation.strip()


def test_rendered_output_shows_remediation_for_a_missing_binary(
    doctor_module, make_context, empty_path
):
    text = rendered(doctor_module, doctor_module.run_doctor(context=make_context())).lower()

    assert any(hint in text for hint in REMEDIATION_HINTS), (
        f"rendered report has no actionable text; expected one of {REMEDIATION_HINTS}"
    )
    for tool in REQUIRED_TOOLS:
        assert tool in text, f"rendered report never mentions the missing {tool!r}"


def test_partially_broken_environment_still_fails(
    doctor_module, make_context, partial_path
):
    """`claude` present, `gh`/`git` absent: still a failure, still specific."""
    report = doctor_module.run_doctor(context=make_context())
    text = rendered(doctor_module, report).lower()

    assert not report.ok
    assert "gh" in text
    assert "git" in text


def test_report_exit_code_is_nonzero_on_failure(doctor_module, make_context, empty_path):
    from panorama.errors import EXIT_OK

    report = doctor_module.run_doctor(context=make_context())
    assert report.exit_code != EXIT_OK


def test_report_exit_code_is_a_known_panorama_code(doctor_module, make_context, empty_path):
    from panorama.errors import EXIT_CLAUDE_INVOCATION_ERROR, EXIT_PREFLIGHT_ERROR

    report = doctor_module.run_doctor(context=make_context())
    assert report.exit_code in {EXIT_PREFLIGHT_ERROR, EXIT_CLAUDE_INVOCATION_ERROR}


def test_raise_for_status_raises_a_panorama_error(doctor_module, make_context, empty_path):
    from panorama.errors import PanoramaError

    report = doctor_module.run_doctor(context=make_context())
    with pytest.raises(PanoramaError):
        report.raise_for_status()


def test_a_failing_check_cannot_be_constructed_without_remediation(doctor_module):
    """The invariant is enforced at construction, not merely by convention."""
    with pytest.raises(ValueError):
        doctor_module.CheckResult("x", doctor_module.STATUS_FAIL, "broken")


def test_run_doctor_never_raises_on_a_broken_environment(
    doctor_module, make_context, empty_path
):
    """A broken environment is an expected state, not a crash."""
    report = doctor_module.run_doctor(context=make_context())
    assert report.results, "run_doctor returned no results at all"


# --------------------------------------------------------------------------
# secrets never render
# --------------------------------------------------------------------------


def test_rendered_report_never_prints_a_secret_from_tool_output(
    doctor_module, make_context, fake_path
):
    """The fake `claude auth status` and `gh auth status` both emit secrets.

    Constraint #5 permits surfacing only ``loggedIn`` / ``authMethod`` /
    ``subscriptionType``. Email, org id, org name and any token must be dropped.
    """
    report = doctor_module.run_doctor(context=make_context())
    assert_no_secrets(rendered(doctor_module, report))


def test_check_results_never_carry_a_secret(doctor_module, make_context, fake_path):
    """Belt and braces: the structured results, not just the rendered text."""
    report = doctor_module.run_doctor(context=make_context())

    for result in report.results:
        assert_no_secrets(result.detail)
        assert_no_secrets(result.remediation or "")


def test_report_dict_never_carries_a_secret(doctor_module, make_context, fake_path):
    """`--json` output is a rendering too."""
    import json

    report = doctor_module.run_doctor(context=make_context())
    assert_no_secrets(json.dumps(report.to_dict()))


@pytest.mark.parametrize("sentinel", SECRET_SENTINELS)
def test_each_known_secret_shape_is_suppressed(
    doctor_module, make_context, fake_path, sentinel
):
    report = doctor_module.run_doctor(context=make_context())
    assert sentinel not in rendered(doctor_module, report)


def test_environment_credentials_are_never_echoed(doctor_module, make_context, fake_path):
    """`doctor` names Anthropic env vars it ignores; it must never read a value."""
    polluted = {
        "PATH": str(fake_path.bin_dir),
        "HOME": str(Path.home()),
        "ANTHROPIC_API_KEY": ENV_SENTINEL,
        "ANTHROPIC_AUTH_TOKEN": ENV_SENTINEL,
        "ANTHROPIC_BASE_URL": ENV_SENTINEL,
        "GITHUB_TOKEN": ENV_SENTINEL,
        "GH_TOKEN": ENV_SENTINEL,
        "AWS_SECRET_ACCESS_KEY": ENV_SENTINEL,
    }
    report = doctor_module.run_doctor(context=make_context(environ=polluted))
    text = rendered(doctor_module, report)

    assert ENV_SENTINEL not in text, "doctor echoed a credential value from the environment"


def test_raw_tool_output_is_never_pasted(doctor_module, make_context, fake_path):
    """Raw `gh` output is forbidden verbatim, secrets or not."""
    report = doctor_module.run_doctor(context=make_context())
    text = rendered(doctor_module, report)

    assert "Token scopes:" not in text, "doctor pasted raw `gh auth status` output"
    assert "Logged in to github.com account" not in text


def test_only_safe_auth_fields_are_declared(doctor_module):
    """The allowlist itself must not grow to include an identifier."""
    assert set(doctor_module.SAFE_AUTH_FIELDS) == {
        "loggedIn",
        "authMethod",
        "subscriptionType",
    }


# --------------------------------------------------------------------------
# credential surface
# --------------------------------------------------------------------------


def test_doctor_never_mutates_claude_auth_state(doctor_module, make_context, fake_path):
    """`auth login`, `auth logout` and `setup-token` are all off limits."""
    doctor_module.run_doctor(context=make_context())

    for call in fake_path.calls:
        argv = call["argv"]
        for mutating in ("login", "logout", "setup-token"):
            assert mutating not in argv, (
                f"doctor invoked a state-changing claude subcommand: {argv}"
            )


def test_doctor_never_passes_the_forbidden_bare_flag(doctor_module, make_context, fake_path):
    doctor_module.run_doctor(context=make_context())

    for call in fake_path.calls:
        assert "--bare" not in call["argv"]
    assert fake_path.violations == [], (
        f"fake claude flagged constraint violations: {fake_path.violations}"
    )


def test_child_environment_carries_no_credential(doctor_module, make_context, fake_path):
    """Every child doctor spawns gets the same stripped environment."""
    doctor_module.run_doctor(context=make_context())

    for call in fake_path.calls:
        for key in call["env"]:
            assert "ANTHROPIC" not in key.upper(), f"{key} reached a doctor child"


def test_minimal_child_env_is_credential_free(doctor_module):
    env = doctor_module.minimal_child_env()

    assert "PATH" in env
    for key in env:
        upper = key.upper()
        for shape in ("ANTHROPIC", "API_KEY", "TOKEN", "SECRET"):
            assert shape not in upper, f"{key} is credential-shaped"


def test_anthropic_env_vars_are_named_but_never_read(doctor_module, make_context, fake_path):
    """A check reports that these are ignored; reporting must not mean reading."""
    polluted = {"PATH": str(fake_path.bin_dir), "ANTHROPIC_API_KEY": ENV_SENTINEL}
    report = doctor_module.run_doctor(context=make_context(environ=polluted))

    for result in report.results:
        assert ENV_SENTINEL not in result.detail
        assert ENV_SENTINEL not in (result.remediation or "")


# --------------------------------------------------------------------------
# CLI surface -- skipped until cli.py is wired to the doctor module
# --------------------------------------------------------------------------


@pytest.fixture
def cli_result(doctor_module, empty_path):
    """Invoke `panorama doctor`, skipping while it is still the stub."""
    from typer.testing import CliRunner

    from panorama.cli import app

    result = CliRunner().invoke(app, ["doctor"])
    if "not implemented" in (result.output or ""):
        pytest.skip("panorama.cli `doctor` is still the scaffold stub; not wired yet")
    return result


def test_cli_doctor_exits_nonzero_on_a_broken_environment(cli_result):
    assert cli_result.exit_code != 0


def test_cli_doctor_renders_remediation(cli_result):
    text = (cli_result.output or "").lower()
    assert any(hint in text for hint in REMEDIATION_HINTS)


def test_cli_doctor_prints_no_secret(cli_result):
    assert_no_secrets(cli_result.output or "")


def test_cli_doctor_help_offers_no_credential_flag():
    """There must be no flag through which a key could be supplied at all."""
    from typer.testing import CliRunner

    from panorama.cli import app

    text = (CliRunner().invoke(app, ["doctor", "--help"]).output or "").lower()
    for forbidden in ("--api-key", "--anthropic", "--token", "--bare"):
        assert forbidden not in text, f"doctor exposes a credential flag: {forbidden}"
