"""Regenerate the retrieval equivalence golden.

    uv run python -m tests.regen_retrieval_golden

Run this only when a change to retrieval behaviour is *intended*, and say what
changed in the commit message. Regenerating it to make a failing test pass
converts the one guard against an accidental behaviour change into a rubber
stamp.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from panorama.fixtures import bootstrap as bootstrap_fixtures
from tests.test_retrieval_equivalence import GOLDEN_PATH, build_snapshot


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo-org"
        bootstrap_fixtures(dest_root=root)
        data = build_snapshot(root)

    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    hits = sum(len(c["hits"]) for c in data.values())
    print(f"wrote {GOLDEN_PATH} — {len(data)} cases, {hits} hits")


if __name__ == "__main__":
    main()
