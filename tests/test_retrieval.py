"""Offline tests for deterministic cross-repository retrieval (S4).

Two layers:

1. **Signal mechanics** — pure-function tests of ``extract_signals``: stopword
   and generated-file filtering, structural detection (fields, routes, added
   functions/classes), comment stripping, polarity accumulation, and the caps
   that keep retrieval an orientation rather than an index. No git, no fixtures.

2. **Ranking over the real fixtures** — the S4 exit criterion. Bootstrap the
   four fixture repos with real git, normalize P1 and P2 through the local PR
   source, and assert retrieval ranks the *consumer* / *shared helper* sibling
   into first place.

The fixture repo names (``acme-*``) appear only here, in a test asserting an
expected outcome. Standing rule: test expectations are not review logic, and
the retrieval code under test contains no fixture name, field name, or finding.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from panorama.fixtures import bootstrap
from panorama.intake import LocalPullRequestSource
from panorama.retrieval import (
    MAX_HITS_PER_SIGNAL_PER_REPO,
    MAX_SIGNALS_SEARCHED,
    MAX_TOTAL_HITS,
    extract_signals,
    retrieve,
)
from panorama.workspace import Workspace

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _diff(body: str) -> str:
    """A minimal but well-formed unified diff around ``body``.

    ``body`` supplies the ``+``/``-`` context lines; the surrounding headers are
    what ``extract_signals`` keys off to find file boundaries.
    """
    return (
        "diff --git a/src/thing.ts b/src/thing.ts\n"
        "--- a/src/thing.ts\n"
        "+++ b/src/thing.ts\n"
        "@@ -1,3 +1,3 @@\n"
    ) + textwrap.dedent(body)


def _tokens(diff: str) -> set[str]:
    return {s.token for s in extract_signals(diff)}


def _by_token(diff: str) -> dict[str, object]:
    return {s.token: s for s in extract_signals(diff)}


# ---------------------------------------------------------------------------
# signal extraction — filtering
# ---------------------------------------------------------------------------


def test_stopwords_and_short_tokens_are_dropped() -> None:
    diff = _diff(
        """\
        +const request = getResponse();
        +if (data) return null;
        """
    )
    tokens = _tokens(diff)
    # `const`, `if`, `return`, `null`, `request`, `data` are all stopwords.
    assert "getResponse" in tokens
    for noise in ("const", "if", "return", "null", "request", "data"):
        assert noise not in tokens


def test_underscore_prefixed_and_numeric_tokens_are_dropped() -> None:
    diff = _diff(
        """\
        +const _private = 42;
        +publicName = 7;
        """
    )
    tokens = _tokens(diff)
    assert "publicName" in tokens
    assert "_private" not in tokens
    assert "42" not in tokens


def test_generated_and_lockfile_hunks_contribute_no_signals() -> None:
    diff = (
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1,1 +1,1 @@\n"
        '+    "resolvedDependency": "1.2.3",\n'
        "diff --git a/dist/bundle.min.js b/dist/bundle.min.js\n"
        "--- a/dist/bundle.min.js\n"
        "+++ b/dist/bundle.min.js\n"
        "@@ -1,1 +1,1 @@\n"
        "+var minifiedGarbageIdentifier=1;\n"
    )
    tokens = _tokens(diff)
    assert "resolvedDependency" not in tokens
    assert "minifiedGarbageIdentifier" not in tokens


def test_binary_file_marker_suppresses_following_lines() -> None:
    diff = (
        "diff --git a/logo.bin b/logo.bin\n"
        "Binary files a/logo.bin and b/logo.bin differ\n"
    )
    assert _tokens(diff) == set()


def test_comment_lines_contribute_no_code_signals() -> None:
    diff = _diff(
        """\
        +// renameThisIdentifier is only mentioned in a comment
        +# pythonCommentIdentifier here too
        """
    )
    tokens = _tokens(diff)
    assert "renameThisIdentifier" not in tokens
    assert "pythonCommentIdentifier" not in tokens


def test_trailing_comment_is_stripped_from_identifier_scan() -> None:
    diff = _diff(
        """\
        +const keepThis = call(); // droppedCommentToken
        """
    )
    tokens = _tokens(diff)
    assert "keepThis" in tokens
    assert "droppedCommentToken" not in tokens


# ---------------------------------------------------------------------------
# signal extraction — structural classification
# ---------------------------------------------------------------------------


def test_public_looking_field_is_highest_weighted() -> None:
    diff = _diff(
        """\
        +  target_url: string;
        """
    )
    signal = _by_token(diff)["target_url"]
    assert signal.kind == "field"
    # A field must outrank any bare identifier so contract-shaped changes lead.
    plain = _by_token(_diff("+  someHelper(x);\n"))["someHelper"]
    assert signal.weight > plain.weight


def test_added_function_and_class_names_are_detected() -> None:
    diff = _diff(
        """\
        +export function handleRedirect(id: string) {}
        +class RedirectController {}
        """
    )
    by = _by_token(diff)
    assert by["handleRedirect"].kind == "function"
    assert by["RedirectController"].kind == "class"


def test_arrow_binding_is_classified_as_function() -> None:
    diff = _diff("+const buildPayload = (x) => x;\n")
    assert _by_token(diff)["buildPayload"].kind == "function"


def test_route_literal_is_detected_and_kept_whole() -> None:
    diff = _diff('+  router.get("/r/:id", handleRedirect);\n')
    by = _by_token(diff)
    assert "/r/:id" in by
    assert by["/r/:id"].kind == "route"


def test_polarity_records_add_and_remove_and_survives_weight_upgrade() -> None:
    # `field` appears first as a bare identifier (removed), then upgraded to a
    # higher-weight field (added). The upgrade must not discard the earlier
    # 'removed' polarity — this is the regression the S4 fix addresses.
    diff = _diff(
        """\
        -  field.doSomething();
        +  field: string;
        """
    )
    signal = _by_token(diff)["field"]
    assert signal.kind == "field"
    assert signal.polarity == frozenset({"added", "removed"})


def test_ranking_puts_heavier_kinds_first() -> None:
    diff = _diff(
        """\
        +  payload_url: string;
        +  somePlainIdentifier();
        """
    )
    order = [s.token for s in extract_signals(diff)]
    assert order.index("payload_url") < order.index("somePlainIdentifier")


def test_extraction_is_deterministic() -> None:
    diff = _diff(
        """\
        +  alpha_field: string;
        +function betaHandler() {}
        +const gammaValue = compute();
        """
    )
    first = extract_signals(diff)
    second = extract_signals(diff)
    assert [s.token for s in first] == [s.token for s in second]


# ---------------------------------------------------------------------------
# ranking over the real fixtures — the S4 exit criterion
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = bootstrap(dest_root=tmp_path / "demo-org").root
    return Workspace(root)


def _retrieve(workspace: Workspace, repo: str, head: str):
    pr = LocalPullRequestSource(workspace.repo(repo).path, "main", head).load()
    return retrieve(pr, workspace)


def test_p1_rename_ranks_the_web_consumer_first(workspace: Workspace) -> None:
    """A field rename in the API must point first at the repo that consumes it."""
    result = _retrieve(workspace, "acme-api", "p1-rename")
    assert result.ranked_repos, "expected at least one ranked sibling"
    assert result.ranked_repos[0].repo == "acme-web"
    # The PR's own repository is never a retrieval target.
    assert all(r.repo != "acme-api" for r in result.ranked_repos)


def test_p2_local_validator_ranks_the_shared_helper_first(workspace: Workspace) -> None:
    """A locally reimplemented validator must point first at the shared helper."""
    result = _retrieve(workspace, "acme-web", "p2-local-validator")
    assert result.ranked_repos, "expected at least one ranked sibling"
    assert result.ranked_repos[0].repo == "acme-shared"
    assert all(r.repo != "acme-web" for r in result.ranked_repos)


def test_convention_docs_are_discovered_from_sibling_content(workspace: Workspace) -> None:
    """Convention documents surface from sibling repositories, never the PR repo."""
    result = _retrieve(workspace, "acme-api", "p1-rename")
    assert "acme-api" not in result.convention_docs
    all_docs = [d for docs in result.convention_docs.values() for d in docs]
    assert all_docs, "expected at least one convention document across siblings"


def test_every_hit_carries_a_locatable_reference(workspace: Workspace) -> None:
    """Each hit is a real repo/path/line with a context window — evidence-ready."""
    result = _retrieve(workspace, "acme-web", "p2-local-validator")
    assert result.hits
    for hit in result.hits:
        assert hit.repo and hit.path and hit.line >= 1
        # The window is the surrounding source lines used later as context.
        assert isinstance(hit.window, tuple)


def test_retrieval_respects_its_caps(workspace: Workspace) -> None:
    """The bounded-orientation caps hold across every fixture branch."""
    for repo, head in (
        ("acme-api", "p1-rename"),
        ("acme-web", "p2-local-validator"),
        ("acme-api", "p3-endpoint-conventions"),
        ("acme-api", "p4-docs-cleanup"),
    ):
        result = _retrieve(workspace, repo, head)
        assert len(result.searched) <= MAX_SIGNALS_SEARCHED
        assert len(result.hits) <= MAX_TOTAL_HITS
        per_signal_repo: dict[tuple[str, str], int] = {}
        for hit in result.hits:
            key = (hit.token, hit.repo)
            per_signal_repo[key] = per_signal_repo.get(key, 0) + 1
        assert all(c <= MAX_HITS_PER_SIGNAL_PER_REPO for c in per_signal_repo.values())


def test_pr_repository_is_never_a_retrieval_target(workspace: Workspace) -> None:
    """Retrieval only ever surfaces *sibling* repositories, by construction."""
    result = _retrieve(workspace, "acme-api", "p3-endpoint-conventions")
    assert all(r.repo != "acme-api" for r in result.ranked_repos)
    assert all(h.repo != "acme-api" for h in result.hits)
