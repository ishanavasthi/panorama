"""Shared pytest fixtures for Panorama tests.

Everything here is fully offline. No test in this suite may reach the network,
the real `claude` CLI, the real `gh`, or anything subscription-backed.

Two things live in this file:

1. **The fake-CLI harness** -- copying the fake executables into a temp dir,
   putting them on ``PATH``, driving them with a scenario file, and reading back
   what they recorded.
2. **A thin adapter layer** over ``panorama.claude_runner`` and
   ``panorama.doctor``. Those modules are owned by other agents and did not
   exist when these tests were written, so the tests bind to them by
   *signature introspection* rather than by hard-coded call shape. If an
   interface lands with different-but-reasonable names, the aliases below are
   the single place to reconcile it.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from tests.fake_claude import ENV_ALLOWLIST, ENV_TOLERATED, RAW_CANARY

FAKE_SOURCE_DIR = Path(__file__).parent / "fake_claude"

#: Executables copied onto the test PATH. `claude` is the one under test;
#: `gh`/`git` exist so `doctor` has something to probe without touching the
#: real tools.
FAKE_EXECUTABLES = ("claude", "gh", "git")


# ==========================================================================
# process helpers
# ==========================================================================


def pid_alive(pid: int) -> bool:
    """True if ``pid`` still exists.

    ``signal 0`` is a permission/existence probe that sends nothing. A zombie
    still answers True, which is why callers should poll rather than sample.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but is owned by someone else / is an unreaped zombie.
        return True
    return True


def wait_pid_gone(pid: int, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """Poll until ``pid`` disappears. Returns False if it outlived ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(interval)
    return not pid_alive(pid)


# ==========================================================================
# the fake CLI harness
# ==========================================================================


class FakeClaude:
    """Handle on one copied-out fake `claude` installation."""

    def __init__(self, bin_dir: Path) -> None:
        self.bin_dir = bin_dir
        self.path = bin_dir / "claude"
        self.gh_path = bin_dir / "gh"
        self.git_path = bin_dir / "git"
        self.scenario_file = bin_dir / "scenario.json"
        self.calls_dir = bin_dir / "calls"
        self.violations_file = bin_dir / "violations.json"
        self.hang_pid_file = bin_dir / "hang_child.pid"
        self.canary = RAW_CANARY

    # -- driving -------------------------------------------------------

    def set_scenario(self, body: dict) -> None:
        """Install a scenario and reset recorded call history."""
        if self.calls_dir.exists():
            shutil.rmtree(self.calls_dir)
        if self.violations_file.exists():
            self.violations_file.unlink()
        if self.hang_pid_file.exists():
            self.hang_pid_file.unlink()
        body = dict(body)
        body.setdefault("canary", RAW_CANARY)
        self.scenario_file.write_text(json.dumps(body, indent=2))

    # -- reading back --------------------------------------------------

    @property
    def calls(self) -> list[dict]:
        """Every recorded invocation, in order."""
        if not self.calls_dir.is_dir():
            return []
        records = []
        for path in sorted(self.calls_dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text()))
            except ValueError:
                # An invocation that claimed a slot but died before writing
                # (the hang scenario is killed mid-flight in some orderings).
                continue
        return records

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def violations(self) -> list[dict]:
        """Hard-constraint violations the fake observed (forbidden flag, or a
        credential-shaped variable in the child environment)."""
        if not self.violations_file.is_file():
            return []
        return json.loads(self.violations_file.read_text())

    def wait_for_hang_child(self, timeout: float = 10.0) -> int:
        """Block until the hang scenario has published its grandchild PID."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.hang_pid_file.is_file():
                text = self.hang_pid_file.read_text().strip()
                if text:
                    return int(text)
            time.sleep(0.02)
        raise AssertionError(
            "fake claude never published a hang-child PID; the hang scenario "
            "did not start"
        )


@pytest.fixture
def fake_bin(tmp_path_factory: pytest.TempPathFactory) -> FakeClaude:
    """A private copy of the fake executables in a temp dir.

    Copied rather than used in place so the scenario file, call records and
    PID file never pollute the repository and never collide between tests.
    """
    bin_dir = tmp_path_factory.mktemp("fakebin")
    for name in FAKE_EXECUTABLES:
        source = FAKE_SOURCE_DIR / name
        if not source.is_file():
            continue
        target = bin_dir / name
        shutil.copy2(source, target)
        target.chmod(0o755)
    return FakeClaude(bin_dir)


@pytest.fixture
def fake_claude_path(fake_bin: FakeClaude, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prepend the fake bin dir to ``PATH`` and return the fake `claude` path.

    ``PATH`` is in the production env allowlist, which is precisely what lets a
    fake on ``PATH`` be found by a child spawned with a stripped environment.
    """
    monkeypatch.setenv("PATH", f"{fake_bin.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake_bin.path


@pytest.fixture
def fake_claude_scenario(fake_bin: FakeClaude, request: pytest.FixtureRequest) -> FakeClaude:
    """Install a scenario on the fake CLI.

    Parametrizable two ways::

        # indirect parametrization -- the param IS the scenario body
        @pytest.mark.parametrize(
            "fake_claude_scenario", [sc.scenario(sc.prose())], indirect=True
        )
        def test_x(fake_claude_scenario): ...

        # or imperatively inside the test
        def test_y(fake_claude_scenario):
            fake_claude_scenario.set_scenario(sc.scenario(sc.cli_error()))

    Returns the :class:`FakeClaude` handle either way, so the test can read
    ``.calls`` / ``.violations`` afterwards.
    """
    param = getattr(request, "param", None)
    if param is not None:
        body = param() if callable(param) else param
        fake_bin.set_scenario(body)
    return fake_bin


# ==========================================================================
# workspace fixture
# ==========================================================================

_DIR_MODE = stat.S_IRWXU  # 0700


class TempWorkspace:
    """A miniature multi-repo organisation workspace.

    Mirrors the real layout: ``<root>/<repo>/...`` with 0700 throughout, since
    it stands in for a directory holding private-repo content.
    """

    def __init__(self, root: Path, repos: dict[str, dict[str, str]]) -> None:
        self.root = root
        self.repos = repos

    def repo_path(self, repo: str) -> Path:
        return self.root / repo

    def file_path(self, repo: str, rel: str) -> Path:
        return self.root / repo / rel

    def line_count(self, repo: str, rel: str) -> int:
        return len(self.file_path(repo, rel).read_text().splitlines())

    # Deterministic anchors so tests never hard-code a guessed line number.
    @property
    def valid_citation(self) -> tuple[str, str, int]:
        """A ``(repo, path, line)`` that really exists and is in bounds."""
        return ("repo-consumer", "src/client.ts", 2)

    @property
    def missing_file_citation(self) -> tuple[str, str, int]:
        """A citation whose file does not exist in the workspace."""
        return ("repo-consumer", "src/does-not-exist.ts", 1)

    @property
    def out_of_bounds_citation(self) -> tuple[str, str, int]:
        """A real file, but a line far past the end of it."""
        return ("repo-consumer", "src/client.ts", 99_999)

    @property
    def foreign_repo_citation(self) -> tuple[str, str, int]:
        """A repo name that is not a child of the workspace at all."""
        return ("repo-not-in-workspace", "src/client.ts", 1)

    @property
    def traversal_citation(self) -> tuple[str, str, int]:
        """A path that tries to escape its repository."""
        return ("repo-consumer", "../../../../etc/passwd", 1)


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> TempWorkspace:
    """A temp multi-repo workspace with 0700 permissions throughout.

    Contents are generic on purpose. Constraint #2 forbids fixture repo/field
    names in production code; these names exist only here in ``tests/``, and
    deliberately do not match the real mock organisation (`acme-*`) so that a
    production module accidentally keying off a name cannot pass by luck.
    """
    root = tmp_path / "workspace"
    root.mkdir(mode=_DIR_MODE)

    repos: dict[str, dict[str, str]] = {
        "repo-owner": {
            "src/handler.ts": (
                "export function buildResponse(input: string) {\n"
                "  return { target_url: input, created_at: Date.now() };\n"
                "}\n"
            ),
            "README.md": "Owner repository.\n",
        },
        "repo-consumer": {
            "src/client.ts": (
                "import { buildResponse } from './api';\n"
                "const { url } = buildResponse('x');\n"
                "export default url;\n"
            ),
            "README.md": "Consumer repository.\n",
        },
        "repo-shared": {
            "lib/validate.ts": (
                "export function validateUrl(candidate: string): boolean {\n"
                "  return candidate.startsWith('https://');\n"
                "}\n"
            ),
        },
        "repo-contracts": {
            "CONVENTIONS.md": "All timestamps are UTC ISO-8601.\n",
        },
    }

    for repo, files in repos.items():
        for rel, content in files.items():
            target = root / repo / rel
            target.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
            target.write_text(content)
            target.chmod(0o600)

    # mkdir's mode is masked by umask; enforce 0700 explicitly afterwards.
    for path in [root, *root.rglob("*")]:
        if path.is_dir():
            path.chmod(_DIR_MODE)

    return TempWorkspace(root, repos)


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside a throwaway cwd.

    ``config.RUN_ARTIFACT_ROOT`` is relative to the working directory, so
    without this a runner test would write ``.panorama/runs/`` into the repo.
    """
    workdir = tmp_path / "cwd"
    workdir.mkdir(mode=_DIR_MODE)
    monkeypatch.chdir(workdir)
    return workdir


# ==========================================================================
# adapter layer over the not-yet-written production modules
# ==========================================================================
#
# INTEGRATOR: if an interface landed with different names, fix the alias tuples
# below and every test keeps working. Nothing else in the suite hard-codes a
# parameter name.

#: Canonical argument -> accepted parameter names, best first.
#:
#: Reconciled against the landed ``ClaudeRunner``:
#:   __init__(executable, model, timeout_seconds, artifact_root)
#:   run(prompt, json_schema, workspace, cwd, validate, repair_instruction,
#:       artifact_dir) -> ClaudeResult(data, telemetry)
#:
#: The alias tuples keep working if any of those names move again.
RUNNER_INIT_ALIASES: dict[str, tuple[str, ...]] = {
    "claude_path": (
        "executable",
        "claude_path",
        "claude_bin",
        "claude_executable",
        "binary",
        "command",
    ),
    "model": ("model",),
    "timeout": ("timeout_seconds", "timeout", "timeout_s", "timeout_sec", "deadline"),
    "run_dir": ("artifact_root", "run_dir", "artifacts_dir", "output_dir"),
    "max_repairs": ("max_repairs", "max_retries", "repair_retries", "retries"),
    # The runner refuses to hand `--add-dir` any path outside its allowed
    # roots, which in production is ~/.panorama/workspaces. Tests build their
    # workspace under pytest's tmp_path, so they must widen the allowlist
    # explicitly -- exactly the opt-in a caller in production would have to
    # make deliberately.
    "allowed_workspace_roots": (
        "allowed_workspace_roots",
        "allowed_roots",
        "workspace_roots",
    ),
}

#: Canonical call argument -> accepted parameter names, best first.
#: ``workspace``/``cwd`` live here as well as in the init map because different
#: designs take them at either point; ``bind_kwargs`` drops whichever the
#: callee does not declare.
RUNNER_CALL_ALIASES: dict[str, tuple[str, ...]] = {
    "prompt": ("prompt", "prompt_text", "user_prompt", "text", "input"),
    "schema": ("json_schema", "schema", "output_schema", "schema_dict"),
    "workspace": ("workspace", "workspace_root", "workspace_dir", "add_dir"),
    "cwd": ("cwd", "working_dir", "workdir", "repo_dir"),
    "validate": ("validate", "validator", "parse", "model_validate"),
    "repair_instruction": ("repair_instruction", "repair_prompt", "repair", "retry_prompt"),
    "artifact_dir": ("artifact_dir", "run_dir", "output_dir"),
}

RUNNER_FACTORY_NAMES = ("ClaudeRunner", "Runner", "ClaudeCLIRunner")
RUNNER_CALL_NAMES = ("run", "run_review", "review", "invoke", "__call__")
RUNNER_FUNCTION_NAMES = ("run_claude", "run_review", "invoke_claude", "run")


def _accepts_var_keyword(sig: inspect.Signature) -> bool:
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def bind_kwargs(
    func: Callable[..., Any],
    values: dict[str, Any],
    aliases: dict[str, tuple[str, ...]],
) -> dict[str, Any]:
    """Map canonical values onto whatever parameter names ``func`` actually has.

    Silently drops anything the callee does not accept, so a leaner signature
    than assumed still works. Unknown-but-accepted extras are never invented.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return dict(values)

    params = set(sig.parameters)
    out: dict[str, Any] = {}
    for canonical, value in values.items():
        if value is None:
            continue
        for name in aliases.get(canonical, (canonical,)):
            if name in params:
                out[name] = value
                break
        else:
            if _accepts_var_keyword(sig):
                out[canonical] = value
    return out


def _first_attr(obj: Any, names: Iterable[str]) -> Any | None:
    for name in names:
        candidate = getattr(obj, name, None)
        if candidate is not None:
            return candidate
    return None


class RunnerAdapter:
    """Calls ``panorama.claude_runner`` without assuming its exact call shape."""

    def __init__(self, module: Any, **init_values: Any) -> None:
        self.module = module
        self.init_values = init_values
        self.instance = None
        self.bound_init: dict[str, Any] = {}

        factory = _first_attr(module, RUNNER_FACTORY_NAMES)
        if factory is not None and inspect.isclass(factory):
            kwargs = bind_kwargs(factory.__init__, init_values, RUNNER_INIT_ALIASES)
            self.bound_init = kwargs
            self.instance = factory(**kwargs)

    def supports(self, canonical: str) -> bool:
        """Whether the implementation accepts a given canonical argument.

        Used to skip -- loudly -- rather than hang: a timeout test against a
        runner with a hard-coded ten-minute deadline and no override would
        block the suite for ten minutes.
        """
        aliases_for = set(
            {**RUNNER_INIT_ALIASES, **RUNNER_CALL_ALIASES}.get(canonical, (canonical,))
        )
        # bound_init is keyed by the *callee's* parameter name, not the
        # canonical one, so compare against the alias set rather than the
        # canonical key.
        if aliases_for & set(self.bound_init):
            return True
        candidates: list[Callable[..., Any]] = []
        if self.instance is not None:
            method = _first_attr(self.instance, RUNNER_CALL_NAMES)
            if method is not None:
                candidates.append(method)
        func = _first_attr(self.module, RUNNER_FUNCTION_NAMES)
        if func is not None:
            candidates.append(func)

        aliases = {**RUNNER_INIT_ALIASES, **RUNNER_CALL_ALIASES}
        wanted = set(aliases.get(canonical, (canonical,)))
        for candidate in candidates:
            try:
                params = inspect.signature(candidate).parameters
            except (TypeError, ValueError):  # pragma: no cover
                continue
            if wanted & set(params) or _accepts_var_keyword(
                inspect.signature(candidate)
            ):
                return True
        return False

    ALL_ALIASES = {**RUNNER_INIT_ALIASES, **RUNNER_CALL_ALIASES}

    def run(self, prompt: str = "review this diff", **overrides: Any) -> Any:
        """Invoke the runner, passing only arguments it actually declares.

        Construction values are re-offered at call time because some designs
        take ``workspace``/``cwd`` per call rather than per instance;
        ``bind_kwargs`` discards whichever the callee does not accept.
        """
        values: dict[str, Any] = {**self.init_values, "prompt": prompt}
        values.update(overrides)

        if self.instance is not None:
            method = _first_attr(self.instance, RUNNER_CALL_NAMES)
            if method is None:
                raise AssertionError(
                    f"no callable among {RUNNER_CALL_NAMES} on "
                    f"{type(self.instance).__name__}"
                )
            return method(**bind_kwargs(method, values, self.ALL_ALIASES))

        func = _first_attr(self.module, RUNNER_FUNCTION_NAMES)
        if func is None:
            raise AssertionError(
                "panorama.claude_runner exposes neither a runner class "
                f"{RUNNER_FACTORY_NAMES} nor a function {RUNNER_FUNCTION_NAMES}"
            )
        return func(**bind_kwargs(func, values, self.ALL_ALIASES))


@pytest.fixture
def claude_runner_module() -> Any:
    """Import ``panorama.claude_runner`` or skip.

    Skips rather than errors so this harness is green before the module it
    exercises exists. Run pytest with ``-ra`` to see the skip reasons -- a
    skipped runner suite means the module has not landed yet, NOT that the
    runner passed.
    """
    return pytest.importorskip(
        "panorama.claude_runner",
        reason="panorama.claude_runner not implemented yet (owned by another agent)",
    )


# A minimal, hand-written Review schema. Deliberately independent of
# ``panorama.models`` so a models regression cannot cascade into the runner
# suite; ``test_models.py`` is what pins the real schema.
REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "verdict", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["approve", "comment", "request_changes"]},
        "findings": {"type": "array", "items": {"type": "object"}},
    },
}

_VERDICTS = {"approve", "comment", "request_changes"}
_SEVERITIES = {"high", "medium", "low"}
_CATEGORIES = {
    "contract_break",
    "duplicate_logic",
    "convention",
    "cross_repo_conflict",
    "single_repo",
}


def validate_review_payload(payload: Any) -> dict:
    """Strict host-side re-validation, raising on any vocabulary violation.

    Stands in for ``Review.model_validate``. It exists because the runner's
    default ``validate`` is the identity function, so without a real validator
    a schema-violating payload would sail through and the "malformed output is
    repaired exactly once" test would never fire.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    for key in ("summary", "verdict", "findings"):
        if key not in payload:
            raise ValueError(f"missing required key {key!r}")
    if payload["verdict"] not in _VERDICTS:
        raise ValueError(f"bad verdict {payload['verdict']!r}")
    if not isinstance(payload["findings"], list):
        raise ValueError("findings is not a list")
    for finding in payload["findings"]:
        if finding.get("severity") not in _SEVERITIES:
            raise ValueError(f"bad severity {finding.get('severity')!r}")
        if finding.get("category") not in _CATEGORIES:
            raise ValueError(f"bad category {finding.get('category')!r}")
    return payload


#: Non-``None`` so the single schema-repair retry is armed by default. The
#: landed runner treats ``repair_instruction=None`` as "never retry", so tests
#: about retry behaviour would silently pass without this.
DEFAULT_REPAIR_INSTRUCTION = (
    "Your previous response did not conform to the schema. Reply with the "
    "structured object only."
)


@pytest.fixture
def make_runner(
    claude_runner_module: Any,
    fake_claude_path: Path,
    tmp_workspace: TempWorkspace,
    isolated_cwd: Path,
) -> Callable[..., RunnerAdapter]:
    """Factory returning a :class:`RunnerAdapter` wired to the fake CLI."""

    def factory(**overrides: Any) -> RunnerAdapter:
        values: dict[str, Any] = {
            "workspace": tmp_workspace.root,
            "allowed_workspace_roots": [tmp_workspace.root],
            "cwd": isolated_cwd,
            "claude_path": str(fake_claude_path),
            "model": "sonnet",
            "timeout": 30.0,
            "run_dir": isolated_cwd / "runs",
            "schema": REVIEW_JSON_SCHEMA,
            "validate": validate_review_payload,
            "repair_instruction": DEFAULT_REPAIR_INSTRUCTION,
        }
        values.update(overrides)
        return RunnerAdapter(claude_runner_module, **values)

    return factory


@pytest.fixture
def doctor_module() -> Any:
    """Import ``panorama.doctor`` or skip."""
    return pytest.importorskip(
        "panorama.doctor",
        reason="panorama.doctor not implemented yet (owned by another agent)",
    )


@pytest.fixture
def models_module() -> Any:
    """Import ``panorama.models`` or skip."""
    return pytest.importorskip(
        "panorama.models",
        reason="panorama.models not implemented yet (owned by another agent)",
    )


# ==========================================================================
# assertions shared across suites
# ==========================================================================

#: Substrings that must never appear in anything rendered to a user. Sourced
#: from the fake `claude auth status` payload and the fake `gh auth status`
#: output, i.e. exactly the fields constraint #5 forbids printing.
SECRET_SENTINELS = (
    "PANORAMA-SECRET-EMAIL",
    "PANORAMA-SECRET-ORG-ID",
    "PANORAMA-SECRET-ORG-NAME",
    "sk-ant-oat01-PANORAMA-SECRET-TOKEN",
    "gho_PANORAMA-SECRET-GH-TOKEN",
)


def assert_no_secrets(text: str, extra: Iterable[str] = ()) -> None:
    """Fail if any known secret-shaped sentinel survives into ``text``."""
    haystack = text or ""
    for sentinel in (*SECRET_SENTINELS, *extra):
        assert sentinel not in haystack, (
            f"secret-shaped value {sentinel!r} leaked into rendered output"
        )


def assert_no_raw_output(text: str) -> None:
    """Fail if the fake CLI's raw-output canary survives into ``text``."""
    assert RAW_CANARY not in (text or ""), (
        "raw claude output leaked into user-facing text (constraint #5)"
    )


def env_of(call: dict) -> dict[str, str]:
    return call.get("env", {})


def unexpected_env_keys(call: dict) -> set[str]:
    """Child env keys outside the allowlist, ignoring benign interpreter noise."""
    return set(env_of(call)) - set(ENV_ALLOWLIST) - set(ENV_TOLERATED)
