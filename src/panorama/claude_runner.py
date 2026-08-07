"""Transport boundary to the locally installed `claude` CLI.

This module knows how to *start a process and get a validated JSON object back*.
It knows nothing about pull requests, reviews, findings, rubrics or fixtures --
the caller supplies the prompt, the JSON Schema and a validator callable, and
gets back whatever that validator produces. Keeping it that dumb is what makes
the security properties auditable: everything about the child process --
argv, environment, filesystem reach, tool set, lifetime -- is decided in this
one file.

Hard constraints enforced here (see ``CLAUDE.md``):

- The only AI integration is the local `claude` executable on its existing
  subscription auth. No SDK, no HTTP endpoint, no API key.
- **No credential is ever passed to the child.** The environment is built from
  a three-name allowlist and then re-checked against a credential-shaped-name
  guard that raises rather than exec'ing.
- Claude gets ``Read``, ``Grep`` and ``Glob`` only. Bash, Write, network tools,
  MCP servers, plugins, hooks, skills, slash commands, operator settings files
  and session persistence are all switched off by flag.
- Raw child output never reaches a user-visible string. It is written to a
  0600 run artifact; exceptions carry a host-authored message plus the artifact
  path.

The exact flag set, exit-code semantics, success detection and timeout recipe
below were derived empirically against Claude Code CLI 2.1.223 during M0; see
``docs/m0-claude-boundary.md``. They are version-specific.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import signal
import stat
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from panorama.config import RUN_ARTIFACT_ROOT, WORKSPACE_ROOT, ensure_dir
from panorama.errors import ClaudeInvocationError

T = TypeVar("T")

DEFAULT_EXECUTABLE = "claude"
DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_SECONDS = 600.0

#: Seconds to wait for the child to reap after SIGTERM before escalating to
#: SIGKILL. The M0 timeout probe reaped well inside this window.
_TERM_GRACE_SECONDS = 5.0

#: The complete set of parent environment variables forwarded to the child.
#: Bisected in M0 against the free ``claude auth status --json`` oracle: PATH +
#: HOME alone yields "Not logged in"; ``USER`` is the non-obvious third name
#: that unlocks the OAuth/keychain lookup (``LOGNAME`` is not a substitute).
ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "USER")

#: Any variable whose *name* matches this is credential-shaped and must never
#: reach the child, regardless of how it got into the environment dict.
_CREDENTIAL_NAME_RE = re.compile(
    r"ANTHROPIC|CLAUDE_.*(KEY|TOKEN|SECRET)|API_?KEY|TOKEN|SECRET|PASSWD|PASSWORD"
    r"|CREDENTIAL|BEARER|SESSION_?KEY|_AUTH$|^AUTH_",
    re.IGNORECASE,
)

#: Any *value* matching this is credential-shaped too. Cheap belt-and-braces
#: against a benignly-named variable carrying a key.
_CREDENTIAL_VALUE_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")

#: Envelope fields we are willing to surface in an error message. They are CLI
#: status tokens, not model text, but they still originate in child stdout, so
#: they are only echoed when they match this conservative shape.
_SAFE_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")


class _Outcome:
    """Internal reason codes. Host-authored, safe to put in messages."""

    TIMEOUT = "timeout"
    STARTUP_ERROR = "startup_error"
    BAD_ENVELOPE = "bad_envelope"
    RUNTIME_ERROR = "runtime_error"
    NO_STRUCTURED_OUTPUT = "no_structured_output"
    SCHEMA_MISMATCH = "schema_mismatch"


#: Reason codes where re-asking the model could plausibly help. Everything else
#: is a process/config/auth failure that a retry would only repeat.
_REPAIRABLE = frozenset({_Outcome.NO_STRUCTURED_OUTPUT, _Outcome.SCHEMA_MISMATCH})


@dataclass(frozen=True)
class ClaudeTelemetry:
    """Non-sensitive facts about an invocation, safe to log.

    ``permission_denial_count`` is deliberately a *count*: the denial entries
    themselves embed absolute operator paths and must never be logged. A
    non-zero count means the model attempted to reach outside its granted
    scope and the CLI refused it.
    """

    attempts: int
    num_turns: int | None
    duration_ms: int | None
    permission_denial_count: int
    artifact_dir: Path


@dataclass(frozen=True)
class ClaudeResult(Generic[T]):
    """A successful invocation: the validated payload plus its telemetry."""

    data: T
    telemetry: ClaudeTelemetry


@dataclass(frozen=True)
class _RawInvocation:
    """One process lifecycle's worth of raw results, before interpretation."""

    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool


def _child_env() -> dict[str, str]:
    """Build the child environment from :data:`ENV_ALLOWLIST` only.

    ``USER`` is resolved from the real uid rather than copied from the parent,
    because a spoofed or absent ``USER`` silently breaks subscription auth
    (M0: ``USER=nobody`` yields ``loggedIn=false``).

    Raises:
        ClaudeInvocationError: if any name or value in the constructed
            environment is credential-shaped. This is a fail-closed guard, not
            a filter: we refuse to exec rather than quietly dropping a variable,
            because a credential appearing here at all means an upstream
            assumption is wrong.
    """
    env: dict[str, str] = {}
    for name in ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env["USER"] = pwd.getpwuid(os.getuid()).pw_name
    if "PATH" not in env:
        env["PATH"] = os.defpath
    if "HOME" not in env:
        env["HOME"] = str(Path.home())

    assert_no_credentials(env)
    return env


def assert_no_credentials(env: Mapping[str, str]) -> None:
    """Raise if ``env`` contains anything credential-shaped.

    Exposed separately so tests (and any future caller that constructs an
    environment) can assert the same invariant. The offending *value* is never
    included in the message.
    """
    for name, value in env.items():
        if _CREDENTIAL_NAME_RE.search(name):
            raise ClaudeInvocationError(
                f"Refusing to start the claude CLI: the child environment would "
                f"include the credential-shaped variable {name!r}. Panorama never "
                f"passes credentials to a child process."
            )
        if _CREDENTIAL_VALUE_RE.search(value):
            raise ClaudeInvocationError(
                f"Refusing to start the claude CLI: the value of {name!r} in the "
                f"child environment is credential-shaped. Panorama never passes "
                f"credentials to a child process."
            )


def _sweep(pgid: int, sig: int) -> None:
    """Signal an entire process group, tolerating an already-dead group.

    ``except OSError`` is deliberate and load-bearing. On macOS, ``killpg``
    against a group that holds only an unreaped zombie raises ``PermissionError``
    (EPERM), not ``ProcessLookupError`` -- catching the narrower exception
    crashes the host on the timeout path (observed in M0 probe C).
    """
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def _write_artifact(directory: Path, name: str, content: str) -> Path:
    """Write ``content`` to ``directory/name`` with 0600 permissions."""
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def safe_token(value: object) -> str | None:
    """Return ``value`` only if it is a short, host-safe status token.

    The single clamp applied to every string that originates in a child process
    before it can reach stdout: bounded length, closed charset. Used for CLI
    envelope tokens (``subtype``, ``terminal_reason``) and for `gh`-derived
    account names, which are equally untrusted.
    """
    if isinstance(value, str) and _SAFE_TOKEN_RE.match(value):
        return value
    return None


#: Internal alias retained so existing call sites read as module-private.
_safe_token = safe_token


class ClaudeRunner:
    """Runs the local `claude` CLI and returns a validated structured object.

    The runner is stateless between calls and safe to reuse. It never inspects
    the semantic content of the prompt or the payload.

    Args:
        executable: Path to (or name on ``PATH`` of) the `claude` binary.
        model: Value for ``--model``. M0 measured ``sonnet`` only.
        timeout_seconds: Hard wall-clock bound per attempt. This is the *only*
            bound on the child: CLI 2.1.223 has no ``--max-turns``, and
            ``--max-budget-usd`` is a post-turn circuit breaker that discards
            the result, so it is deliberately not used.
        artifact_root: Directory under which per-run artifact directories are
            created. Defaults to ``.panorama/runs``.
    """

    def __init__(
        self,
        *,
        executable: str | Path = DEFAULT_EXECUTABLE,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        artifact_root: Path | None = None,
        allowed_workspace_roots: Sequence[Path] | None = None,
    ) -> None:
        self.executable = str(executable)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.artifact_root = artifact_root if artifact_root is not None else RUN_ARTIFACT_ROOT
        roots = allowed_workspace_roots if allowed_workspace_roots is not None else [WORKSPACE_ROOT]
        self.allowed_workspace_roots = tuple(Path(root).expanduser().resolve() for root in roots)

    # -- workspace containment ----------------------------------------------

    def _checked_workspace(self, workspace: Path) -> Path:
        """Resolve ``workspace`` and prove it is inside an allowed root.

        ``--add-dir`` is the *only* thing that widens the child's filesystem
        reach beyond its cwd, so it is the one argument that decides how much
        private data a review can touch. Restricting the tool set to
        Read/Grep/Glob bounds what the model can *do*; this bounds what it can
        do it *to*. Unvalidated, ``--add-dir /`` or ``--add-dir ~`` would hand
        the model ``~/.ssh`` and ``~/.config/gh/hosts.yml`` -- the very GitHub
        credential that is supposed to stay with `gh`.

        Resolution happens before the containment test so that a symlink
        pointing out of the workspace cannot smuggle the child elsewhere.
        """
        resolved = Path(workspace).expanduser().resolve()
        if not resolved.is_dir():
            raise ClaudeInvocationError(
                f"The review workspace {resolved} is not an existing directory."
            )
        for root in self.allowed_workspace_roots:
            if resolved == root or root in resolved.parents:
                return resolved
        allowed = ", ".join(str(root) for root in self.allowed_workspace_roots)
        raise ClaudeInvocationError(
            "Refusing to grant the review process filesystem access outside the "
            f"Panorama workspace. Requested {resolved}; allowed roots: {allowed}."
        )

    # -- argv ---------------------------------------------------------------

    def build_argv(self, *, json_schema: Mapping[str, Any], workspace: Path) -> list[str]:
        """Return the exact argv for one invocation.

        Never assembled into a shell string; ``shell=True`` is never used
        anywhere in this module.

        Flag rationale, each item empirically established in M0:

        - ``-p``: non-interactive print mode, the only mode that cannot block
          on a permission prompt.
        - ``--output-format json``: one JSON envelope on stdout carrying
          ``is_error``/``subtype``/``stop_reason``/``permission_denials``, so no
          prose is ever parsed.
        - ``--json-schema``: takes an inline JSON *string*, not a path, and adds
          the already-parsed ``structured_output`` key to the envelope.
        - ``--tools Read,Grep,Glob``: the only flag that actually shrinks the
          tool set. ``--allowedTools`` restricts nothing;
          ``--disallowedTools`` is an unbounded denylist. The CLI auto-injects
          ``StructuredOutput`` when ``--json-schema`` is present, so restricting
          tools does not break structured output.
        - ``--add-dir``: the sole grant of filesystem reach beyond cwd. Pass the
          workspace *root* so one Grep spans every sibling repository.
        - ``--safe-mode``: kills ``CLAUDE.md`` discovery, skills, plugins and
          plugin hooks -- including a ``CLAUDE.md`` planted *inside* the added
          directory, which is the prompt-injection case that matters.
        - ``--setting-sources ""``: kills user/project/local ``settings.json``,
          which ``--safe-mode`` does not suppress. The empty string is the
          accepted value; the literal ``none`` exits 1.
        - ``--strict-mcp-config`` / ``--mcp-config {"mcpServers":{}}``: no MCP
          servers from any source. The second is redundant today and kept as
          insurance against a behaviour change.
        - ``--disable-slash-commands``: no operator custom commands.
        - ``--no-session-persistence``: no transcript ``.jsonl`` under
          ``~/.claude/projects/``, which would otherwise persist verbatim
          repository source to disk.

        Deliberately omitted: ``--permission-mode`` (the default already fails
        closed non-interactively and denies out-of-scope reads without
        hanging), ``--max-budget-usd``, and ``--allowedTools``/
        ``--disallowedTools``.

        Deliberately forbidden: ``--bare``, which forces
        ``ANTHROPIC_API_KEY``/``apiKeyHelper`` auth and never reads OAuth or the
        keychain. Using it would both break subscription auth and violate the
        no-credentials constraint.
        """
        schema_arg = json.dumps(json_schema, separators=(",", ":"), sort_keys=True)
        return [
            self.executable,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--json-schema",
            schema_arg,
            "--tools",
            "Read,Grep,Glob",
            "--add-dir",
            str(self._checked_workspace(workspace)),
            "--safe-mode",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--no-session-persistence",
        ]

    # -- process lifecycle --------------------------------------------------

    def _spawn(self, argv: list[str], *, cwd: Path, prompt: str) -> _RawInvocation:
        """Run one child to completion or to the timeout, leaving no orphans.

        The prompt goes on **stdin**, always. Three independent reasons:
        ``--tools`` is variadic and ``--debug`` takes an optional argument, so
        either can silently swallow a positional prompt; argv is bounded by
        ``ARG_MAX`` (1 MiB here, with a separate 128 KiB per-string cap on
        Linux) and a real review prompt is large; and ``claude -p`` with no
        positional prompt reads stdin and completes normally. stdin must always
        be wired explicitly or the CLI stalls ~3s warning that no stdin arrived.
        """
        env = _child_env()
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, never shell=True
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ClaudeInvocationError(
                f"The claude CLI was not found (looked for {self.executable!r}). "
                f"Install Claude Code and make sure it is on PATH, then re-run "
                f"`panorama doctor`."
            ) from exc
        except OSError as exc:
            raise ClaudeInvocationError(
                "Could not start the claude CLI. Run `panorama doctor` to check "
                "the local Claude Code installation."
            ) from exc

        # Cache the group id immediately: it is unavailable once the child is
        # reaped, and start_new_session=True guarantees the group is the
        # child's own, so signalling it cannot reach Panorama itself.
        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            pgid = process.pid

        timed_out = False
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _sweep(pgid, signal.SIGTERM)
            try:
                # Reap *between* TERM and KILL. This is what avoids the
                # zombie-only group whose killpg returns EPERM on macOS.
                stdout, stderr = process.communicate(timeout=_TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _sweep(pgid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            _sweep(pgid, signal.SIGKILL)  # stragglers in the group only

        return _RawInvocation(
            stdout=stdout or "",
            stderr=stderr or "",
            returncode=process.returncode,
            timed_out=timed_out,
        )

    # -- envelope interpretation -------------------------------------------

    def _interpret(
        self,
        raw: _RawInvocation,
        validate: Callable[[Any], T],
    ) -> tuple[T | None, str | None, str, dict[str, Any]]:
        """Turn a raw invocation into ``(value, reason, detail, envelope)``.

        ``reason`` is ``None`` on success. ``detail`` is a host-authored,
        already-redacted clause suitable for an error message. ``envelope`` is
        the parsed stdout object, or ``{}`` if it could not be parsed.

        Success detection is exactly one test: the string key
        ``structured_output`` is **present** in the parsed envelope. Nothing
        else signals it. In particular, a model that refuses or emits
        non-conforming output still exits 0 with ``is_error=false`` and
        ``subtype="success"``; only key absence catches that.
        """
        # ORDERING IS LOAD-BEARING: this branch must stay ahead of the
        # envelope parse below. M0's written spec claimed stdout is empty on
        # timeout; that is FALSE on CLI 2.1.223. On SIGTERM the CLI flushes a
        # complete, parseable envelope with is_error=true and
        # subtype="error_during_execution". If the is_error branch ran first,
        # every timeout would be reported as a runtime error and the operator
        # would be told to check whether they are signed in -- the wrong
        # diagnostic for what is really a scope or latency problem.
        if raw.timed_out:
            return (
                None,
                _Outcome.TIMEOUT,
                f"it exceeded the {self.timeout_seconds:.0f}s time limit and was terminated",
                {},
            )

        if not raw.stdout.strip():
            # stderr may carry an operator path or a flag error; never echo it.
            return (
                None,
                _Outcome.STARTUP_ERROR,
                f"it produced no output (exit code {raw.returncode})",
                {},
            )

        try:
            envelope = json.loads(raw.stdout)
        except json.JSONDecodeError:
            return (None, _Outcome.BAD_ENVELOPE, "its output was not valid JSON", {})

        if not isinstance(envelope, dict):
            return (None, _Outcome.BAD_ENVELOPE, "its output was not a JSON object", {})

        if envelope.get("is_error"):
            subtype = _safe_token(envelope.get("subtype"))
            terminal = _safe_token(envelope.get("terminal_reason"))
            parts = [p for p in (subtype, terminal) if p]
            detail = "it reported an error"
            if parts:
                detail += f" ({', '.join(parts)})"
            return (None, _Outcome.RUNTIME_ERROR, detail, envelope)

        if "structured_output" not in envelope:
            return (
                None,
                _Outcome.NO_STRUCTURED_OUTPUT,
                "it returned prose instead of the requested structured result",
                envelope,
            )

        # `structured_output` is already parsed (a dict, not a JSON string):
        # no double decode. `result` holds the same payload as a string and is
        # a last-resort fallback only.
        payload = envelope["structured_output"]
        try:
            value = validate(payload)
        except Exception:
            # Host-side re-validation is mandatory: CLI-side enforcement of
            # value-level constraints (enum/minimum/maximum) was never observed.
            # The validator's message can quote model text, so it is discarded.
            return (
                None,
                _Outcome.SCHEMA_MISMATCH,
                "its structured result did not match the expected schema",
                envelope,
            )

        return (value, None, "", envelope)

    # -- public API ---------------------------------------------------------

    def run(
        self,
        *,
        prompt: str,
        json_schema: Mapping[str, Any],
        workspace: Path,
        cwd: Path | None = None,
        # Required, deliberately. The CLI was never observed enforcing
        # value-level schema constraints (enum/minimum/maximum), so host-side
        # re-validation is the only thing standing between model output and
        # code that trusts it. An identity default made that skippable by
        # omission.
        validate: Callable[[Any], T],
        repair_instruction: str | None = None,
        artifact_dir: Path | None = None,
    ) -> ClaudeResult[T]:
        """Invoke the CLI once, retrying at most once for malformed output.

        Args:
            prompt: The complete prompt, delivered on stdin. The runner does
                not interpret it and does not know what it contains.
            json_schema: JSON Schema object for the expected result.
            workspace: Root directory granted via ``--add-dir``. Pass the
                workspace root, not an individual repository, so one search can
                span siblings.
            cwd: Working directory for the child. Defaults to ``workspace``.
            validate: Callable that converts the raw structured payload into
                the caller's type, raising on a bad payload. Defaults to
                identity, which yields the raw object.
            repair_instruction: Appended to the prompt on the single retry.
                If ``None``, no retry is attempted. The retry fires *only* for
                a missing or non-conforming structured result -- never for a
                timeout, startup failure or runtime error, which a retry would
                merely repeat.
            artifact_dir: Where raw child output is stored (0600 files). A
                fresh directory under ``artifact_root`` is created if omitted.

        Returns:
            A :class:`ClaudeResult` carrying the validated value and telemetry.

        Raises:
            ClaudeInvocationError: on any failure. The message is host-authored
                and redacted; it names the run-artifact directory so an operator
                can inspect the raw output themselves, but never embeds child
                stdout or stderr.
        """
        target_cwd = cwd if cwd is not None else workspace
        artifacts = self._prepare_artifact_dir(artifact_dir)
        argv = self.build_argv(json_schema=json_schema, workspace=workspace)

        attempt_prompt = prompt
        last_reason = _Outcome.STARTUP_ERROR
        last_detail = "it did not run"
        attempt = 0
        max_attempts = 2 if repair_instruction is not None else 1

        for attempt in range(1, max_attempts + 1):  # noqa: B007 - `attempt` used after loop
            raw = self._spawn(argv, cwd=target_cwd, prompt=attempt_prompt)
            self._persist(artifacts, attempt, raw)
            value, reason, detail, envelope = self._interpret(raw, validate)

            if reason is None:
                telemetry = ClaudeTelemetry(
                    attempts=attempt,
                    num_turns=_as_int(envelope.get("num_turns")),
                    duration_ms=_as_int(envelope.get("duration_ms")),
                    permission_denial_count=_denial_count(envelope),
                    artifact_dir=artifacts,
                )
                # `value` is only None when `reason` is set.
                return ClaudeResult(data=value, telemetry=telemetry)  # type: ignore[arg-type]

            last_reason, last_detail = reason, detail
            if reason not in _REPAIRABLE or attempt >= max_attempts:
                break
            # One schema-repair retry, and only one. The repair instruction
            # restates the output contract; it must not change the task.
            attempt_prompt = f"{prompt}\n\n{repair_instruction}"

        raise ClaudeInvocationError(
            self._failure_message(last_reason, last_detail, artifacts, repaired=attempt > 1)
        )

    # -- helpers ------------------------------------------------------------

    def _prepare_artifact_dir(self, artifact_dir: Path | None) -> Path:
        directory = (
            artifact_dir
            if artifact_dir is not None
            else self.artifact_root / f"claude-{uuid.uuid4().hex[:12]}"
        )
        ensure_dir(directory)
        try:
            os.chmod(directory, 0o700)
            mode = stat.S_IMODE(directory.stat().st_mode)
        except OSError as exc:
            raise ClaudeInvocationError(
                f"Could not secure the run artifact directory {directory}."
            ) from exc
        if mode != stat.S_IRWXU:
            # This directory is about to receive raw child output, which for a
            # real review is verbatim private-repo source. Failing open here
            # would leave it readable at an unknown mode, so refuse instead.
            raise ClaudeInvocationError(
                f"Run artifact directory {directory} is {mode:04o}, expected 0700."
            )
        return directory

    def _persist(self, artifacts: Path, attempt: int, raw: _RawInvocation) -> None:
        """Store raw child output under 0600, so it never has to be printed."""
        _write_artifact(artifacts, f"attempt-{attempt}.stdout.json", raw.stdout)
        _write_artifact(artifacts, f"attempt-{attempt}.stderr.txt", raw.stderr)

    def _failure_message(self, reason: str, detail: str, artifacts: Path, *, repaired: bool) -> str:
        """Compose a host-authored failure message.

        Every fragment here is a literal written in this file plus the artifact
        path. No child output is interpolated.
        """
        malformed = (
            "The schema-repair retry did not help. "
            if repaired
            else "The result could not be used. "
        ) + "Retry the run; if it persists, the review scope is likely too large."
        remedies = {
            _Outcome.TIMEOUT: ("Retry, narrow the review scope, or raise the timeout."),
            _Outcome.STARTUP_ERROR: (
                "Run `panorama doctor` to check that the claude CLI is installed and logged in."
            ),
            _Outcome.BAD_ENVELOPE: (
                "This usually means an incompatible claude CLI version. Run `panorama doctor`."
            ),
            _Outcome.RUNTIME_ERROR: (
                "Run `panorama doctor`; if the CLI reports it is not logged in, "
                "sign in to Claude Code and retry."
            ),
            _Outcome.NO_STRUCTURED_OUTPUT: malformed,
            _Outcome.SCHEMA_MISMATCH: malformed,
        }
        remedy = remedies.get(reason, "Run `panorama doctor`.")
        return (
            f"The claude CLI did not return a usable review: {detail} "
            f"[{reason}]. {remedy} Raw output was kept at {artifacts} (0600) "
            f"and is not shown here."
        )


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _denial_count(envelope: Mapping[str, Any]) -> int:
    """Count permission denials without ever reading their contents.

    A non-empty ``permission_denials`` list means an in-scope tool was aimed
    out of scope and the CLI refused. The entries themselves embed absolute
    operator paths, so only the count leaves this function.
    """
    denials = envelope.get("permission_denials")
    return len(denials) if isinstance(denials, list) else 0
