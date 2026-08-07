"""Path constants and directory-permission helpers for Panorama.

Two roots matter:

- ``WORKSPACE_ROOT``: where cloned/checked-out sibling repos and PR checkouts
  live across runs, under the user's home directory.
- ``RUN_ARTIFACT_ROOT``: where per-run artifacts (logs, findings, raw
  responses) are written, relative to the current working directory.

Both are created with owner-only (0700) permissions since they may contain
private-repo content.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

WORKSPACE_ROOT = Path.home() / ".panorama" / "workspaces"
RUN_ARTIFACT_ROOT = Path(".panorama") / "runs"

_DIR_MODE = stat.S_IRWXU  # 0700: read/write/execute for owner only


def ensure_dir(path: Path) -> Path:
    """Create ``path`` and every missing parent with 0700 permissions.

    Returns the path for convenient chaining. Existing directories are left
    with whatever permissions they already have.

    ``Path.mkdir(parents=True, mode=...)`` applies ``mode`` to the LEAF ONLY --
    CPython creates intermediate parents with the process umask, which is
    typically 0755. Creating ``.panorama/runs/<run-id>`` that way leaves the
    two enclosing directories world-readable, so run ids, run counts and timing
    are enumerable by any other local user even though the leaf and the files
    inside it are locked down. Walk the chain and create each missing level
    explicitly instead.
    """
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:  # reached the filesystem root
            break
        probe = probe.parent

    for directory in reversed(missing):
        directory.mkdir(mode=_DIR_MODE, exist_ok=True)
        # mkdir's mode is masked by the umask, so restate it unconditionally.
        os.chmod(directory, _DIR_MODE)

    return path
