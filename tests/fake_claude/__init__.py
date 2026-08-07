"""Scenario presets for the fake `claude` executable.

The executable itself (``tests/fake_claude/claude``) is a standalone script: it
never imports this package, because it runs under a stripped environment from a
copied temp directory. The two communicate only through the ``scenario.json``
sidecar file, whose shape is documented at the top of the script.

This module builds those scenario dicts so tests read declaratively.
"""

from __future__ import annotations

from typing import Any

#: Embedded in every raw channel the fake writes (prose ``result``, stderr,
#: runtime-error text). Nothing user-facing -- above all no exception message --
#: may ever contain it. See ``test_claude_runner.py``.
RAW_CANARY = "PANORAMA-RAW-CANARY-DO-NOT-LEAK"

#: The environment allowlist proven sufficient for subscription OAuth in M0.
#: ``USER`` is the non-obvious one: without it the CLI reports "Not logged in".
ENV_ALLOWLIST = frozenset({"PATH", "HOME", "USER"})

#: Variables some Python builds inject on macOS regardless of the passed env.
#: Tolerated in the "child env is minimal" assertion; they carry no credential.
ENV_TOLERATED = frozenset(
    {"__PYVENV_LAUNCHER__", "__CF_USER_TEXT_ENCODING", "PYTHONHOME", "PYTHONPATH", "LC_CTYPE"}
)

Behaviour = dict[str, Any]
Scenario = dict[str, Any]


# --------------------------------------------------------------------------
# behaviours
# --------------------------------------------------------------------------


def structured(payload: dict, **overrides: Any) -> Behaviour:
    """rc 0, envelope carries a parsed ``structured_output``."""
    return {"mode": "structured", "payload": payload, **overrides}


def prose(result: str | None = None, **overrides: Any) -> Behaviour:
    """rc 0, ``is_error`` false, subtype "success" -- and NO ``structured_output``.

    The model-refusal trap. Nothing but key absence detects it.
    """
    behaviour: Behaviour = {"mode": "prose"}
    if result is not None:
        behaviour["result"] = result
    behaviour.update(overrides)
    return behaviour


def unparseable(stdout: str | None = None, **overrides: Any) -> Behaviour:
    """rc 0, stdout is not JSON at all."""
    behaviour: Behaviour = {"mode": "unparseable"}
    if stdout is not None:
        behaviour["stdout"] = stdout
    behaviour.update(overrides)
    return behaviour


def cli_error(rc: int = 1, stderr: str | None = None, **overrides: Any) -> Behaviour:
    """Non-zero exit with an EMPTY stdout -- bad flag / malformed schema."""
    behaviour: Behaviour = {"mode": "empty_stdout", "rc": rc}
    if stderr is not None:
        behaviour["stderr"] = stderr
    behaviour.update(overrides)
    return behaviour


def runtime_error(
    subtype: str = "error_max_budget_usd",
    terminal_reason: str = "budget_exhausted",
    **overrides: Any,
) -> Behaviour:
    """rc 1, envelope present with ``is_error`` true and no ``result`` key."""
    return {
        "mode": "runtime_error",
        "subtype": subtype,
        "terminal_reason": terminal_reason,
        **overrides,
    }


def auth_failure(**overrides: Any) -> Behaviour:
    """rc 1, subtype "success" but ``is_error`` true. Never trust subtype alone."""
    return {"mode": "auth_failure", **overrides}


def hang(ignore_sigterm: bool = False, sleep: float = 300, **overrides: Any) -> Behaviour:
    """Block forever after spawning a grandchild in the same process group.

    ``ignore_sigterm`` makes the grandchild survive the SIGTERM sweep so that
    only the final SIGKILL reaps it -- the escalation branch M0 never exercised
    end to end.
    """
    return {
        "mode": "hang",
        "ignore_sigterm": ignore_sigterm,
        "sleep": sleep,
        **overrides,
    }


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------


def scenario(*calls: Behaviour, default: Behaviour | None = None, **extra: Any) -> Scenario:
    """Compose per-invocation behaviours into a scenario file body.

    ``calls[i]`` drives invocation ``i``; ``default`` drives anything past the
    end. Leaving ``default`` as ``None`` repeats the last listed behaviour,
    which is what "the repair retry also fails" scenarios want.
    """
    body: Scenario = {"canary": RAW_CANARY, "calls": list(calls)}
    if default is not None:
        body["default"] = default
    elif calls:
        body["default"] = calls[-1]
    body.update(extra)
    return body


# --------------------------------------------------------------------------
# review payload builders (valid against the Review/Finding/Evidence contract)
# --------------------------------------------------------------------------


def evidence(repo: str, path: str, line: int) -> dict:
    return {"repo": repo, "path": path, "line": line}


def finding(
    *,
    severity: str = "high",
    category: str = "contract_break",
    title: str = "Response field rename breaks a sibling consumer",
    rationale: str = "The changed field is destructured by another repository.",
    pr_path: str | None = None,
    pr_line: int | None = None,
    evidence_refs: list[dict] | None = None,
    recommendation: str | None = "Coordinate the rename with the consumer repository.",
    confidence: str = "high",
) -> dict:
    return {
        "severity": severity,
        "category": category,
        "title": title,
        "rationale": rationale,
        "pr_path": pr_path,
        "pr_line": pr_line,
        "evidence": evidence_refs if evidence_refs is not None else [],
        "recommendation": recommendation,
        "confidence": confidence,
    }


def review(
    *,
    summary: str = "One cross-repository contract break detected.",
    verdict: str = "request_changes",
    findings: list[dict] | None = None,
) -> dict:
    return {
        "summary": summary,
        "verdict": verdict,
        "findings": findings if findings is not None else [],
    }


def review_with_bad_enum() -> dict:
    """Schema-shaped but value-invalid: the CLI was never observed enforcing
    enums, which is exactly why the host must re-validate."""
    return {
        "summary": f"Bad enum payload {RAW_CANARY}",
        "verdict": "definitely_not_a_verdict",
        "findings": [finding(severity="catastrophic", category="made_up_category")],
    }
