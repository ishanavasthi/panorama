"""Exception hierarchy and exit codes for Panorama.

All exit-code constants live here so callers (CLI entry points, tests) have a
single source of truth instead of scattering magic numbers.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_GENERAL_ERROR = 1
EXIT_PREFLIGHT_ERROR = 2
EXIT_CLAUDE_INVOCATION_ERROR = 3
EXIT_VALIDATION_ERROR = 4


class PanoramaError(Exception):
    """Base class for all Panorama-raised errors.

    Subclasses should set ``exit_code`` to one of the ``EXIT_*`` constants
    above so the CLI layer can translate any Panorama error into a process
    exit code without inspecting error text.
    """

    exit_code: int = EXIT_GENERAL_ERROR

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PreflightError(PanoramaError):
    """Raised when a preflight check fails (e.g. missing `claude`/`gh`/`git`,
    unwritable workspace directories)."""

    exit_code = EXIT_PREFLIGHT_ERROR


class ClaudeInvocationError(PanoramaError):
    """Raised when invoking the local `claude` CLI fails or returns output
    that cannot be used (non-zero exit, malformed response, timeout)."""

    exit_code = EXIT_CLAUDE_INVOCATION_ERROR


class ValidationError(PanoramaError):
    """Raised when input or output data fails validation (e.g. a malformed
    PR reference, or a Claude response that fails schema validation)."""

    exit_code = EXIT_VALIDATION_ERROR
