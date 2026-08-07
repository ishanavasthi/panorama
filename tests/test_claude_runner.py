"""Offline tests for the `claude` CLI boundary.

Every test here drives the fake `claude` executable from ``tests/fake_claude/``.
Nothing reaches the network, the real CLI, or the subscription.

The properties under test are the ones M0 identified as load-bearing:

* exit 0 does **not** mean success -- only ``structured_output`` presence does;
* malformed output earns exactly one repair retry, never an unbounded loop;
* a timeout kills the whole process **group**, leaving no orphan;
* raw child output never reaches a user-facing string;
* the child environment carries no credential.
"""

from __future__ import annotations

from typing import Any

import pytest

from panorama.errors import ClaudeInvocationError, PanoramaError
from tests import fake_claude as sc
from tests.conftest import (
    RunnerAdapter,
    assert_no_raw_output,
    unexpected_env_keys,
    wait_pid_gone,
)

CRED_SENTINEL = "PANORAMA-PARENT-CREDENTIAL-VALUE"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def structured_of(result: Any) -> dict:
    """Normalise whatever the runner returns into a plain payload dict.

    Tolerates three plausible return shapes: the payload dict itself, a
    Pydantic ``Review``, or a small result object wrapping one of those.
    """
    if isinstance(result, dict):
        # A raw envelope would be a bug; the runner must hand back the payload.
        assert "structured_output" not in result, (
            "runner returned the raw CLI envelope; it must return the extracted "
            "structured payload only"
        )
        return result
    for attr in ("structured_output", "payload", "review", "data", "value"):
        inner = getattr(result, attr, None)
        if inner is not None:
            return structured_of(inner)
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        return dump()
    raise AssertionError(f"cannot extract a review payload from {type(result).__name__}")


def error_text(exc: BaseException) -> str:
    """Everything a user could plausibly see from a raised error."""
    parts = [str(exc), repr(exc)]
    message = getattr(exc, "message", None)
    if message:
        parts.append(str(message))
    cause = exc.__cause__
    if cause is not None:
        parts.extend([str(cause), repr(cause)])
    context = exc.__context__
    if context is not None:
        parts.extend([str(context), repr(context)])
    return "\n".join(parts)


def valid_payload(workspace) -> dict:
    repo, path, line = workspace.valid_citation
    return sc.review(
        findings=[
            sc.finding(
                pr_path="src/handler.ts",
                pr_line=2,
                evidence_refs=[sc.evidence(repo, path, line)],
            )
        ]
    )


# --------------------------------------------------------------------------
# success path
# --------------------------------------------------------------------------


def test_success_returns_parsed_structured_payload(
    make_runner, fake_claude_scenario, tmp_workspace
):
    """A structured envelope yields the parsed payload, not prose, not raw JSON."""
    payload = valid_payload(tmp_workspace)
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(payload)))

    result = make_runner().run()
    got = structured_of(result)

    assert got["summary"] == payload["summary"]
    assert got["verdict"] == payload["verdict"]
    assert len(got["findings"]) == 1

    evidence = got["findings"][0]["evidence"][0]
    repo, path, line = tmp_workspace.valid_citation
    assert (evidence["repo"], evidence["path"], evidence["line"]) == (repo, path, line)

    assert fake_claude_scenario.call_count == 1, "a successful call must not retry"


def test_success_makes_exactly_one_invocation(make_runner, fake_claude_scenario, tmp_workspace):
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(valid_payload(tmp_workspace))))
    make_runner().run()
    assert fake_claude_scenario.call_count == 1


def test_prompt_is_delivered_on_stdin(make_runner, fake_claude_scenario, tmp_workspace):
    """argv is unsafe for the prompt: ``--tools`` is variadic and ARG_MAX bites."""
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(valid_payload(tmp_workspace))))
    marker = "PANORAMA-PROMPT-MARKER-9137"

    make_runner().run(prompt=f"review this diff {marker}")

    call = fake_claude_scenario.calls[0]
    assert marker in call["stdin"], "prompt must be written to the child's stdin"
    assert not any(marker in arg for arg in call["argv"]), (
        "prompt must never be passed as an argv element"
    )


def test_invocation_never_uses_the_forbidden_bare_flag(
    make_runner, fake_claude_scenario, tmp_workspace
):
    """``--bare`` would force API-key auth and never read the OAuth keychain."""
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(valid_payload(tmp_workspace))))
    make_runner().run()

    call = fake_claude_scenario.calls[0]
    assert "--bare" not in call["argv"]
    assert fake_claude_scenario.violations == []


def _flag_value(argv: list[str], flag: str) -> str | None:
    """Return the argument following ``flag`` in ``argv``, or None if absent."""
    for index, token in enumerate(argv):
        if token == flag:
            return argv[index + 1] if index + 1 < len(argv) else None
    return None


#: Tools Claude is granted. CLAUDE.md constraint 4 makes this an exact set, not
#: a lower bound: Bash/Write/Edit/WebFetch/WebSearch/Task must be unreachable.
GRANTED_TOOLS = {"Read", "Grep", "Glob"}

#: Flags that switch off a whole class of operator-controlled input. Each one
#: closes an injection or leak path proven live in M0; a silent regression on
#: any of them is invisible in output, so it has to be pinned here.
REQUIRED_ISOLATION_FLAGS = (
    "-p",
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
)


def test_claude_is_granted_exactly_read_grep_glob(make_runner, fake_claude_scenario, tmp_workspace):
    """Constraint 4: Read/Grep/Glob ONLY.

    Asserted against the argv the child actually received, not against
    ``build_argv``, so a future refactor that stops routing through the builder
    cannot quietly widen the tool set. ``--tools`` is the only flag that really
    shrinks it -- ``--allowedTools`` restricts nothing -- so this assertion is
    the single thing standing between the model and a shell.
    """
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(valid_payload(tmp_workspace))))
    make_runner().run()

    argv = fake_claude_scenario.calls[0]["argv"]
    tools_arg = _flag_value(argv, "--tools")
    assert tools_arg is not None, "--tools is absent: the child gets the FULL tool set"

    granted = {tool.strip() for tool in tools_arg.split(",") if tool.strip()}
    assert granted == GRANTED_TOOLS, (
        f"tool set drifted from {sorted(GRANTED_TOOLS)} to {sorted(granted)}"
    )

    forbidden = {"Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task", "NotebookEdit"}
    assert not (granted & forbidden), f"dangerous tools granted: {sorted(granted & forbidden)}"


def test_config_isolation_flags_are_all_present(make_runner, fake_claude_scenario, tmp_workspace):
    """Constraint 6: repository content is untrusted data.

    ``--safe-mode`` is what stopped a ``CLAUDE.md`` planted inside the reviewed
    workspace from reaching the model in M0. Dropping it re-opens direct prompt
    injection from any repository under review, and nothing in the output would
    look different.
    """
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(valid_payload(tmp_workspace))))
    make_runner().run()

    argv = fake_claude_scenario.calls[0]["argv"]
    for flag in REQUIRED_ISOLATION_FLAGS:
        assert flag in argv, f"isolation flag {flag} is missing from the invocation"

    # Operator settings files carry permission rules and env blocks that
    # --safe-mode does NOT suppress. The empty string means "no sources"; the
    # literal "none" is rejected by the CLI with exit 1.
    assert _flag_value(argv, "--setting-sources") == "", (
        "--setting-sources must be the empty string to disable user/project/local settings"
    )
    assert _flag_value(argv, "--output-format") == "json", (
        "structured output detection depends on the single-envelope json format"
    )


# --------------------------------------------------------------------------
# the one schema-repair retry
# --------------------------------------------------------------------------

# Only *model-authored* malformation is repairable: the model produced the
# wrong thing and can be asked again. A stdout that is not JSON at all is a
# broken or version-mismatched CLI, and re-prompting would only repeat it --
# see ``test_unparseable_stdout_is_not_repaired`` below, which pins that
# distinction so it cannot be blurred by accident.
MALFORMED_BEHAVIOURS = {
    # rc 0, is_error false, subtype "success" -- and no structured_output key.
    "missing_structured_output": sc.prose(),
    # structured_output present but the values violate the enum contract.
    "schema_violation": sc.structured(sc.review_with_bad_enum()),
}


@pytest.mark.parametrize("kind", sorted(MALFORMED_BEHAVIOURS))
def test_malformed_output_retries_exactly_once_then_raises(
    kind, make_runner, fake_claude_scenario
):
    """Persistent malformed output: one repair retry, then a clean failure.

    Two invocations, never three. An unbounded repair loop against a
    subscription is the failure mode this guards.
    """
    behaviour = MALFORMED_BEHAVIOURS[kind]
    fake_claude_scenario.set_scenario(sc.scenario(behaviour, default=behaviour))

    with pytest.raises(ClaudeInvocationError):
        make_runner().run()

    assert fake_claude_scenario.call_count == 2, (
        f"expected exactly one repair retry for {kind}, saw "
        f"{fake_claude_scenario.call_count} invocation(s)"
    )


@pytest.mark.parametrize("kind", sorted(MALFORMED_BEHAVIOURS))
def test_repair_retry_succeeds_when_the_second_response_conforms(
    kind, make_runner, fake_claude_scenario, tmp_workspace
):
    """The retry is a real second chance, not a formality before failing."""
    payload = valid_payload(tmp_workspace)
    fake_claude_scenario.set_scenario(
        sc.scenario(MALFORMED_BEHAVIOURS[kind], sc.structured(payload))
    )

    result = make_runner().run()

    assert structured_of(result)["summary"] == payload["summary"]
    assert fake_claude_scenario.call_count == 2


def test_unparseable_stdout_is_not_repaired(make_runner, fake_claude_scenario):
    """Non-JSON stdout fails immediately rather than consuming the retry.

    The boundary being drawn: a *missing or non-conforming structured result*
    is the model's mistake and is worth one more turn; stdout that is not JSON
    at all means the CLI itself is broken or version-mismatched, and a retry
    would only pay for the same failure twice.
    """
    fake_claude_scenario.set_scenario(sc.scenario(sc.unparseable(), default=sc.unparseable()))

    with pytest.raises(ClaudeInvocationError):
        make_runner().run()

    assert fake_claude_scenario.call_count == 1, (
        "a non-JSON envelope is a CLI fault, not a schema fault; it must not "
        "consume the repair retry"
    )


def test_repair_is_disabled_when_no_instruction_is_supplied(
    make_runner, fake_claude_scenario
):
    """Opting out of repair must really mean one attempt."""
    runner = make_runner(repair_instruction=None)
    if not runner.supports("repair_instruction"):
        pytest.skip("runner has no opt-out for the repair retry")

    fake_claude_scenario.set_scenario(sc.scenario(sc.prose(), default=sc.prose()))

    with pytest.raises(ClaudeInvocationError):
        runner.run(repair_instruction=None)

    assert fake_claude_scenario.call_count == 1


def test_repair_retry_sends_a_second_prompt(make_runner, fake_claude_scenario, tmp_workspace):
    """The repair attempt must actually re-prompt, not replay an empty stdin."""
    fake_claude_scenario.set_scenario(
        sc.scenario(sc.prose(), sc.structured(valid_payload(tmp_workspace)))
    )
    make_runner().run()

    calls = fake_claude_scenario.calls
    assert len(calls) == 2
    assert calls[1]["stdin_len"] > 0, "repair retry sent an empty prompt"


# --------------------------------------------------------------------------
# CLI-level errors
# --------------------------------------------------------------------------


def test_nonzero_exit_with_empty_stdout_raises(make_runner, fake_claude_scenario):
    """Bad flag / malformed --json-schema: pre-flight failure, stdout empty."""
    fake_claude_scenario.set_scenario(sc.scenario(sc.cli_error(rc=1)))

    with pytest.raises(ClaudeInvocationError):
        make_runner().run()


def test_startup_error_is_not_retried(make_runner, fake_claude_scenario):
    """A CLI-level failure is not malformed *output*; repairing it is pointless."""
    fake_claude_scenario.set_scenario(sc.scenario(sc.cli_error(rc=1)))

    with pytest.raises(ClaudeInvocationError):
        make_runner().run()

    assert fake_claude_scenario.call_count == 1, (
        "a startup error must not consume the schema-repair retry"
    )


def test_runtime_abort_envelope_raises(make_runner, fake_claude_scenario):
    """rc 1 with a parseable envelope whose ``is_error`` is true and no ``result``."""
    fake_claude_scenario.set_scenario(
        sc.scenario(sc.runtime_error("error_max_budget_usd", "budget_exhausted"))
    )

    with pytest.raises(ClaudeInvocationError):
        make_runner().run()


def test_auth_failure_is_detected_despite_success_subtype(make_runner, fake_claude_scenario):
    """``subtype`` says "success" while ``is_error`` is true. Trust ``is_error``."""
    fake_claude_scenario.set_scenario(sc.scenario(sc.auth_failure()))

    with pytest.raises(ClaudeInvocationError):
        make_runner().run()


def test_errors_are_panorama_errors_with_the_right_exit_code(
    make_runner, fake_claude_scenario
):
    fake_claude_scenario.set_scenario(sc.scenario(sc.cli_error(rc=1)))

    with pytest.raises(PanoramaError) as caught:
        make_runner().run()

    from panorama.errors import EXIT_CLAUDE_INVOCATION_ERROR

    assert caught.value.exit_code == EXIT_CLAUDE_INVOCATION_ERROR


# --------------------------------------------------------------------------
# timeout and process-group cleanup
# --------------------------------------------------------------------------


def _runner_with_short_timeout(make_runner, seconds: float = 3.0) -> RunnerAdapter:
    runner = make_runner(timeout=seconds)
    if not runner.supports("timeout"):
        pytest.skip(
            "runner exposes no timeout override; refusing to block the suite on "
            "a hard-coded deadline"
        )
    return runner


@pytest.mark.parametrize(
    "ignore_sigterm",
    [
        pytest.param(False, id="child_dies_on_sigterm"),
        pytest.param(True, id="child_survives_sigterm_needs_sigkill"),
    ],
)
def test_timeout_kills_the_process_group_and_leaves_no_orphan(
    ignore_sigterm, make_runner, fake_claude_scenario
):
    """The decisive cleanup test.

    The fake spawns a grandchild in its own process group and then blocks. A
    runner that kills only its direct child leaves that grandchild running
    forever -- the exact bug a parent-only kill hides, because the real CLI's
    own helpers are sub-second and usually die on their own.

    The ``ignore_sigterm`` variant additionally forces the SIGKILL escalation
    branch, which M0 wrote but never exercised end to end.
    """
    fake_claude_scenario.set_scenario(
        sc.scenario(sc.hang(ignore_sigterm=ignore_sigterm, sleep=600))
    )
    runner = _runner_with_short_timeout(make_runner, seconds=3.0)

    with pytest.raises(ClaudeInvocationError):
        runner.run()

    child_pid = fake_claude_scenario.wait_for_hang_child()
    assert wait_pid_gone(child_pid, timeout=15.0), (
        f"grandchild pid {child_pid} survived the timeout: the runner killed "
        "only its direct child, not the process group"
    )


def test_timeout_error_leaks_no_raw_output(make_runner, fake_claude_scenario):
    fake_claude_scenario.set_scenario(sc.scenario(sc.hang(sleep=600)))
    runner = _runner_with_short_timeout(make_runner, seconds=3.0)

    with pytest.raises(ClaudeInvocationError) as caught:
        runner.run()

    assert_no_raw_output(error_text(caught.value))

    child_pid = fake_claude_scenario.wait_for_hang_child()
    wait_pid_gone(child_pid, timeout=15.0)


def test_timeout_does_not_consume_the_repair_retry(make_runner, fake_claude_scenario):
    """A hang is not malformed output; retrying it would double a 10-minute wait."""
    fake_claude_scenario.set_scenario(sc.scenario(sc.hang(sleep=600)))
    runner = _runner_with_short_timeout(make_runner, seconds=3.0)

    with pytest.raises(ClaudeInvocationError):
        runner.run()

    assert fake_claude_scenario.call_count == 1, (
        "a timeout must not be retried as a schema repair"
    )

    child_pid = fake_claude_scenario.wait_for_hang_child()
    wait_pid_gone(child_pid, timeout=15.0)


# --------------------------------------------------------------------------
# constraint #5 -- raw child output never becomes user-facing text
# --------------------------------------------------------------------------

LEAKY_SCENARIOS = {
    "prose_refusal": sc.scenario(sc.prose(), default=sc.prose()),
    "unparseable": sc.scenario(sc.unparseable(), default=sc.unparseable()),
    "cli_error": sc.scenario(sc.cli_error(rc=1)),
    "runtime_error": sc.scenario(sc.runtime_error()),
    "schema_violation": sc.scenario(sc.structured(sc.review_with_bad_enum())),
}


@pytest.mark.parametrize("kind", sorted(LEAKY_SCENARIOS))
def test_raw_claude_output_never_appears_in_the_exception(
    kind, make_runner, fake_claude_scenario
):
    """Every raw channel the fake writes carries a canary. None may escape.

    Raw output originates in untrusted repository data and may embed absolute
    operator paths; the host must emit fixed sentences plus, at most, the
    closed-vocabulary ``subtype``/``terminal_reason``.
    """
    fake_claude_scenario.set_scenario(LEAKY_SCENARIOS[kind])

    with pytest.raises(PanoramaError) as caught:
        make_runner().run()

    assert_no_raw_output(error_text(caught.value))


def test_permission_denial_entries_never_reach_the_exception(
    make_runner, fake_claude_scenario
):
    """Denial records embed absolute operator paths; only a count may be logged."""
    secret_path = "/Users/PANORAMA-OPERATOR/private/outside/secret.txt"
    fake_claude_scenario.set_scenario(
        sc.scenario(
            sc.prose(
                envelope_overrides={
                    "permission_denials": [
                        {"tool_name": "Read", "tool_input": {"file_path": secret_path}}
                    ]
                }
            )
        )
    )

    with pytest.raises(PanoramaError) as caught:
        make_runner().run()

    assert secret_path not in error_text(caught.value)
    assert "PANORAMA-OPERATOR" not in error_text(caught.value)


# --------------------------------------------------------------------------
# constraint #1 -- no credential ever reaches the child
# --------------------------------------------------------------------------


def test_child_environment_is_limited_to_the_allowlist(
    make_runner, fake_claude_scenario, tmp_workspace, monkeypatch
):
    """The parent is deliberately polluted with credential-shaped variables."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", CRED_SENTINEL)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CRED_SENTINEL)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.invalid")
    monkeypatch.setenv("GITHUB_TOKEN", CRED_SENTINEL)
    monkeypatch.setenv("GH_TOKEN", CRED_SENTINEL)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", CRED_SENTINEL)

    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(valid_payload(tmp_workspace))))
    make_runner().run()

    call = fake_claude_scenario.calls[0]
    env = call["env"]

    assert unexpected_env_keys(call) == set(), (
        "child environment carries variables outside the {PATH, HOME, USER} "
        f"allowlist: {sorted(unexpected_env_keys(call))}"
    )
    assert fake_claude_scenario.violations == [], (
        f"fake claude flagged constraint violations: {fake_claude_scenario.violations}"
    )
    assert CRED_SENTINEL not in "\n".join(env.values()), (
        "a parent credential value was propagated into the child environment"
    )


def test_child_environment_still_authenticates(
    make_runner, fake_claude_scenario, tmp_workspace
):
    """USER is the non-obvious allowlist member: without it the CLI is logged out."""
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(valid_payload(tmp_workspace))))
    make_runner().run()

    env = fake_claude_scenario.calls[0]["env"]
    for required in ("PATH", "HOME", "USER"):
        assert env.get(required), f"{required} missing from the child environment"
    assert env["USER"] not in ("", "nobody"), "USER must carry the real account name"


def test_no_api_key_flag_is_passed(make_runner, fake_claude_scenario, tmp_workspace):
    """No flag through which a credential could enter the child.

    Only flag-shaped tokens are inspected. Matching against the whole joined
    argv gives false positives, because argv legitimately carries temp paths
    that can contain almost any substring -- including this test's own name.
    """
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(valid_payload(tmp_workspace))))
    make_runner().run()

    flags = [a.lower() for a in fake_claude_scenario.calls[0]["argv"] if a.startswith("-")]
    for forbidden in ("api-key", "api_key", "apikey", "auth-token", "bare", "token"):
        offenders = [flag for flag in flags if forbidden in flag]
        assert offenders == [], f"forbidden auth surface {forbidden!r} in argv: {offenders}"


# --------------------------------------------------------------------------
# invalid citations -- transported intact, rejected later by the validator
# --------------------------------------------------------------------------

BAD_CITATIONS = ("missing_file_citation", "out_of_bounds_citation", "foreign_repo_citation")


@pytest.mark.parametrize("citation_kind", BAD_CITATIONS)
def test_invalid_citations_survive_transport_for_the_validator(
    citation_kind, make_runner, fake_claude_scenario, tmp_workspace
):
    """The runner is transport plus schema. Evidence truth is the validator's job.

    A schema-valid payload whose evidence points at a nonexistent file, an
    out-of-bounds line, or a repo outside the workspace must come back intact
    so the host validator can discard it -- silently dropping it in the runner
    would hide the discard count the report is required to state.
    """
    repo, path, line = getattr(tmp_workspace, citation_kind)
    payload = sc.review(
        findings=[sc.finding(evidence_refs=[sc.evidence(repo, path, line)])]
    )
    fake_claude_scenario.set_scenario(sc.scenario(sc.structured(payload)))

    got = structured_of(make_runner().run())

    evidence = got["findings"][0]["evidence"][0]
    assert (evidence["repo"], evidence["path"], evidence["line"]) == (repo, path, line)


def test_workspace_fixture_backs_the_citation_anchors(tmp_workspace):
    """Guards the fixture itself, so a citation test cannot pass vacuously."""
    repo, path, line = tmp_workspace.valid_citation
    target = tmp_workspace.file_path(repo, path)
    assert target.is_file()
    assert 1 <= line <= tmp_workspace.line_count(repo, path)

    missing_repo, missing_path, _ = tmp_workspace.missing_file_citation
    assert not tmp_workspace.file_path(missing_repo, missing_path).exists()

    oob_repo, oob_path, oob_line = tmp_workspace.out_of_bounds_citation
    assert oob_line > tmp_workspace.line_count(oob_repo, oob_path)

    foreign_repo, _, _ = tmp_workspace.foreign_repo_citation
    assert not tmp_workspace.repo_path(foreign_repo).exists()


def test_workspace_is_owner_only(tmp_workspace):
    """0700 throughout: the workspace stands in for private-repo content."""
    import stat as _stat

    for path in [tmp_workspace.root, *tmp_workspace.root.rglob("*")]:
        if path.is_dir():
            mode = _stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o700, f"{path} is {oct(mode)}, expected 0o700"
