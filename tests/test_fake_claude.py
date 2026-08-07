"""Self-tests for the offline harness.

Everything else in this suite is only as trustworthy as the fake CLI it runs
against, and the runner tests skip entirely until ``panorama.claude_runner``
lands. These tests have no such dependency: they exercise the fake directly
through the M0 invocation recipe, so the harness is proven on day one.

The reference spawn helper below is deliberately a faithful copy of the recipe
from ``docs/m0-claude-boundary.md``. It is the executable statement of what the
real runner is expected to do, and the negative-control test proves the hang
scenario can actually catch a parent-only kill.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests import fake_claude as sc
from tests.conftest import pid_alive, wait_pid_gone

MINIMAL_ENV_KEYS = ("PATH", "HOME", "USER")


# --------------------------------------------------------------------------
# reference invocation recipe
# --------------------------------------------------------------------------


def minimal_env(extra_path: Path | None = None) -> dict[str, str]:
    """Exactly {PATH, HOME, USER}. USER is what unlocks the OAuth lookup."""
    import pwd

    path = os.environ.get("PATH", "/usr/bin:/bin")
    if extra_path is not None:
        path = f"{extra_path}{os.pathsep}{path}"
    return {
        "PATH": path,
        "HOME": os.environ.get("HOME", str(Path.home())),
        "USER": pwd.getpwuid(os.getuid()).pw_name,
    }


def sweep(pgid: int, sig: int) -> None:
    """Signal a whole process group, tolerating an empty or zombie-only group.

    ``except OSError`` rather than ``except ProcessLookupError``: on macOS a
    killpg against a group holding only unreaped zombies raises EPERM, which is
    a PermissionError. Probe C crashed the harness on exactly this.
    """
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


class Outcome:
    def __init__(self, rc: int | None, stdout: str, stderr: str, timed_out: bool) -> None:
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    @property
    def envelope(self) -> dict:
        return json.loads(self.stdout)


def run_fake(
    exe: Path,
    prompt: str = "review this",
    timeout: float = 30.0,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    args: list[str] | None = None,
    kill_parent_only: bool = False,
) -> Outcome:
    argv = [str(exe), "-p", "--output-format", "json", *(args or [])]

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env if env is not None else minimal_env(exe.parent),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # mandatory: else killpg would hit pytest itself
    )
    pgid = os.getpgid(proc.pid)  # cache now; unavailable once reaped

    timed_out = False
    try:
        out, err = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if kill_parent_only:
            # The naive implementation the hang scenario exists to catch.
            proc.kill()
            out, err = proc.communicate()
        else:
            sweep(pgid, signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=5)  # reap BETWEEN term and kill
            except subprocess.TimeoutExpired:
                sweep(pgid, signal.SIGKILL)
                out, err = proc.communicate()
            sweep(pgid, signal.SIGKILL)  # stragglers only

    return Outcome(proc.returncode, out or "", err or "", timed_out)


# --------------------------------------------------------------------------
# envelope shapes
# --------------------------------------------------------------------------


def test_structured_success_envelope(fake_bin):
    payload = sc.review(findings=[sc.finding(evidence_refs=[sc.evidence("r", "p.ts", 3)])])
    fake_bin.set_scenario(sc.scenario(sc.structured(payload)))

    outcome = run_fake(fake_bin.path)
    envelope = outcome.envelope

    assert outcome.rc == 0
    assert outcome.stderr == ""
    assert "structured_output" in envelope
    assert envelope["structured_output"] == payload
    assert envelope["is_error"] is False
    assert envelope["subtype"] == "success"
    assert envelope["stop_reason"] == "tool_use"
    # structured_output arrives already parsed, not as a JSON string.
    assert isinstance(envelope["structured_output"], dict)


def test_structured_output_matches_the_result_string(fake_bin):
    """``result`` holds the identical payload as a string -- a last-resort fallback."""
    payload = sc.review()
    fake_bin.set_scenario(sc.scenario(sc.structured(payload)))

    envelope = run_fake(fake_bin.path).envelope
    assert json.loads(envelope["result"]) == envelope["structured_output"]


def test_prose_refusal_exits_zero_without_structured_output(fake_bin):
    """The critical trap: nothing but key absence signals this failure."""
    fake_bin.set_scenario(sc.scenario(sc.prose()))

    outcome = run_fake(fake_bin.path)
    envelope = outcome.envelope

    assert outcome.rc == 0
    assert envelope["is_error"] is False
    assert envelope["subtype"] == "success"
    assert "structured_output" not in envelope
    assert envelope["stop_reason"] == "end_turn"


def test_unparseable_stdout(fake_bin):
    fake_bin.set_scenario(sc.scenario(sc.unparseable()))
    outcome = run_fake(fake_bin.path)

    assert outcome.rc == 0
    with pytest.raises(ValueError):
        json.loads(outcome.stdout)


def test_cli_error_has_empty_stdout(fake_bin):
    fake_bin.set_scenario(sc.scenario(sc.cli_error(rc=1)))
    outcome = run_fake(fake_bin.path)

    assert outcome.rc == 1
    assert outcome.stdout == ""
    assert sc.RAW_CANARY in outcome.stderr  # the canary lives on the raw channel


def test_runtime_error_envelope_omits_result(fake_bin):
    fake_bin.set_scenario(sc.scenario(sc.runtime_error("error_max_budget_usd", "budget_exhausted")))
    outcome = run_fake(fake_bin.path)
    envelope = outcome.envelope

    assert outcome.rc == 1
    assert envelope["is_error"] is True
    assert envelope["subtype"] == "error_max_budget_usd"
    assert envelope["terminal_reason"] == "budget_exhausted"
    assert "result" not in envelope, "the real CLI omits the key entirely, not null"


def test_auth_failure_lies_in_subtype(fake_bin):
    fake_bin.set_scenario(sc.scenario(sc.auth_failure()))
    outcome = run_fake(fake_bin.path)
    envelope = outcome.envelope

    assert outcome.rc == 1
    assert envelope["subtype"] == "success"
    assert envelope["is_error"] is True
    assert "Not logged in" in envelope["result"]


def test_schema_violating_payload_is_still_structurally_delivered(fake_bin):
    """The CLI does not enforce enums, which is why the host must re-validate."""
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review_with_bad_enum())))
    envelope = run_fake(fake_bin.path).envelope

    assert "structured_output" in envelope
    assert envelope["structured_output"]["verdict"] == "definitely_not_a_verdict"


# --------------------------------------------------------------------------
# scenario sequencing -- the basis of the repair-retry assertion
# --------------------------------------------------------------------------


def test_calls_advance_through_the_scenario(fake_bin):
    payload = sc.review(summary="second attempt conformed")
    fake_bin.set_scenario(sc.scenario(sc.prose(), sc.structured(payload)))

    first = run_fake(fake_bin.path).envelope
    second = run_fake(fake_bin.path).envelope

    assert "structured_output" not in first
    assert second["structured_output"]["summary"] == "second attempt conformed"
    assert fake_bin.call_count == 2


def test_default_behaviour_repeats_past_the_listed_calls(fake_bin):
    fake_bin.set_scenario(sc.scenario(sc.prose(), default=sc.prose()))

    for _ in range(3):
        assert "structured_output" not in run_fake(fake_bin.path).envelope
    assert fake_bin.call_count == 3


def test_setting_a_scenario_resets_call_history(fake_bin):
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review())))
    run_fake(fake_bin.path)
    assert fake_bin.call_count == 1

    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review())))
    assert fake_bin.call_count == 0


def test_invocations_are_recorded_with_argv_cwd_and_stdin(fake_bin, tmp_path):
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review())))
    workdir = tmp_path / "neutral-cwd"
    workdir.mkdir()

    run_fake(fake_bin.path, prompt="MARKER-4471", cwd=workdir, args=["--model", "sonnet"])

    call = fake_bin.calls[0]
    assert call["stdin"] == "MARKER-4471"
    assert "--model" in call["argv"] and "sonnet" in call["argv"]
    assert Path(call["cwd"]).resolve() == workdir.resolve()


# --------------------------------------------------------------------------
# environment and constraint detection
# --------------------------------------------------------------------------


def test_fake_records_a_minimal_environment(fake_bin):
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review())))
    run_fake(fake_bin.path, env=minimal_env(fake_bin.bin_dir))

    env = fake_bin.calls[0]["env"]
    for key in MINIMAL_ENV_KEYS:
        assert env.get(key)
    assert fake_bin.violations == []


def test_fake_flags_a_credential_shaped_variable(fake_bin):
    """Proves the constraint detector is armed, not merely silent."""
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review())))
    env = minimal_env(fake_bin.bin_dir)
    env["ANTHROPIC_API_KEY"] = "leaked"

    run_fake(fake_bin.path, env=env)

    violations = [v["violation"] for v in fake_bin.violations]
    assert "credential-shaped-env:ANTHROPIC_API_KEY" in violations


def test_fake_flags_the_forbidden_bare_flag(fake_bin):
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review())))
    run_fake(fake_bin.path, args=["--bare"])

    violations = [v["violation"] for v in fake_bin.violations]
    assert "forbidden-arg:--bare" in violations


def test_auth_status_surfaces_secrets_for_the_doctor_tests(fake_bin):
    """The doctor secret tests are only meaningful if secrets are really present."""
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review())))
    outcome = run_fake(fake_bin.path, args=["auth", "status", "--json"])
    payload = json.loads(outcome.stdout)

    assert payload["loggedIn"] is True
    assert payload["authMethod"] == "subscription"
    assert "PANORAMA-SECRET-EMAIL" in payload["email"]
    assert payload["accessToken"].startswith("sk-ant-oat01-")


def test_stream_json_emits_an_init_event(fake_bin):
    """The doctor smoke run asserts on this event after every CLI upgrade."""
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review())))
    outcome = run_fake(
        fake_bin.path, args=["--output-format", "stream-json", "--verbose"]
    )

    events = [json.loads(line) for line in outcome.stdout.splitlines() if line.strip()]
    init = events[0]

    assert init["type"] == "system" and init["subtype"] == "init"
    assert set(init["tools"]) == {"Glob", "Grep", "Read", "StructuredOutput"}
    assert init["mcp_servers"] == []
    assert init["plugins"] == []
    assert init["slash_commands"] == []
    assert init["apiKeySource"] == "none"


# --------------------------------------------------------------------------
# the hang scenario and process-group cleanup
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ignore_sigterm",
    [
        pytest.param(False, id="child_dies_on_sigterm"),
        pytest.param(True, id="child_survives_sigterm_needs_sigkill"),
    ],
)
def test_group_kill_reaps_the_grandchild(fake_bin, ignore_sigterm):
    fake_bin.set_scenario(sc.scenario(sc.hang(ignore_sigterm=ignore_sigterm, sleep=600)))

    outcome = run_fake(fake_bin.path, timeout=1.0)
    child_pid = fake_bin.wait_for_hang_child()

    assert outcome.timed_out
    assert outcome.stdout == ""
    assert wait_pid_gone(child_pid, timeout=15.0), (
        f"grandchild {child_pid} survived the process-group sweep"
    )


def test_sigterm_yields_a_positive_143_not_a_negative_signal(fake_bin):
    """Branching on ``returncode < 0`` misclassifies every timeout."""
    fake_bin.set_scenario(sc.scenario(sc.hang(sleep=600)))

    outcome = run_fake(fake_bin.path, timeout=1.0)
    fake_bin.wait_for_hang_child()

    assert outcome.rc == 143, f"expected 128+15, got {outcome.rc}"
    assert outcome.rc > 0


def test_parent_only_kill_leaves_an_orphan(fake_bin):
    """Negative control.

    Without this, a passing group-kill test proves nothing: the grandchild
    might simply be dying on its own. Here the naive parent-only kill is used
    deliberately, and the grandchild MUST survive it -- which is what makes the
    real cleanup test meaningful.
    """
    fake_bin.set_scenario(sc.scenario(sc.hang(ignore_sigterm=True, sleep=600)))

    outcome = run_fake(fake_bin.path, timeout=1.0, kill_parent_only=True)
    child_pid = fake_bin.wait_for_hang_child()

    assert outcome.timed_out
    try:
        # Give the naive path the same grace the real one gets, then check.
        time.sleep(1.0)
        assert pid_alive(child_pid), (
            "the hang scenario's grandchild died without a group kill; the "
            "cleanup test would pass vacuously"
        )
    finally:
        # Clean up the orphan this test intentionally created.
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass
        wait_pid_gone(child_pid, timeout=10.0)


def test_hang_child_is_in_the_same_process_group_as_the_fake(fake_bin):
    """Group membership is what makes killpg the only sufficient cleanup."""
    fake_bin.set_scenario(sc.scenario(sc.hang(sleep=600)))

    run_fake(fake_bin.path, timeout=1.0)
    child_pid = fake_bin.wait_for_hang_child()
    wait_pid_gone(child_pid, timeout=15.0)

    call = fake_bin.calls[0]
    assert call["pgid"] == call["pid"], (
        "the fake was not a process-group leader; start_new_session was not used"
    )


# --------------------------------------------------------------------------
# fixtures themselves
# --------------------------------------------------------------------------


def test_fake_claude_path_fixture_puts_the_fake_first(fake_claude_path):
    import shutil

    resolved = shutil.which("claude")
    assert resolved is not None
    assert Path(resolved).resolve() == Path(fake_claude_path).resolve()


def test_fake_is_copied_out_of_the_repository(fake_bin):
    """Scenario files and call records must never land in the working tree."""
    repo_fake_dir = Path(__file__).parent / "fake_claude"
    assert fake_bin.bin_dir.resolve() != repo_fake_dir.resolve()
    assert not (repo_fake_dir / "scenario.json").exists()
    assert not (repo_fake_dir / "calls").exists()


def test_default_scenario_needs_no_scenario_file(fake_bin):
    """A bare copy still answers, so a fixture ordering slip fails loudly, not weirdly."""
    envelope = run_fake(fake_bin.path).envelope
    assert "structured_output" in envelope


def test_fake_executables_are_runnable(fake_bin):
    for exe in (fake_bin.path, fake_bin.gh_path, fake_bin.git_path):
        assert os.access(exe, os.X_OK), f"{exe} is not executable"
        result = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        assert result.stdout.strip()


def test_fake_gh_refuses_networked_subcommands(fake_bin):
    result = subprocess.run(
        [str(fake_bin.gh_path), "api", "/user"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode != 0


def test_fake_git_refuses_networked_subcommands(fake_bin):
    result = subprocess.run(
        [str(fake_bin.git_path), "clone", "https://example.invalid/x"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
