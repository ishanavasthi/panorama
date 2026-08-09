"""Offline tests for review delivery: the idempotent `--post` comment (S9).

Two layers:

1. **`PRCommentPoster` against a fake `gh`** — the head-SHA re-read, the
   marker-based create-vs-update decision, the final secret screen, and failure
   handling, all driven by a scenario sidecar. No network, no token.

2. **CLI `--post` wiring** — that the GitHub path re-checks the head SHA, aborts
   on a moved PR, and reports the create/update outcome. The poster is faked so
   nothing leaves the process.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from panorama.delivery import (
    COMMENT_MARKER,
    PRCommentPoster,
    assert_postable,
    build_comment_body,
)
from panorama.errors import IntakeError, ValidationError
from tests import fake_gh as gh

FAKE_GH_SOURCE = Path(__file__).parent / "fake_gh" / "gh"


# ---------------------------------------------------------------------------
# fake gh harness (reads back what was posted)
# ---------------------------------------------------------------------------


class FakeGh:
    def __init__(self, bin_dir: Path) -> None:
        self.bin_dir = bin_dir
        self.path = bin_dir / "gh"
        self.sidecar = bin_dir / "gh_scenario.json"
        self.posted = bin_dir / "posted.json"

    def set_scenario(self, body: dict) -> None:
        self.sidecar.write_text(json.dumps(body))

    def posted_record(self) -> dict | None:
        return json.loads(self.posted.read_text()) if self.posted.is_file() else None


@pytest.fixture
def fake_gh_bin(tmp_path: Path) -> FakeGh:
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    target = bin_dir / "gh"
    shutil.copy2(FAKE_GH_SOURCE, target)
    target.chmod(0o755)
    return FakeGh(bin_dir)


def _poster(fake: FakeGh) -> PRCommentPoster:
    return PRCommentPoster("acme", "acme-api", 7, gh_path=str(fake.path))


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_build_comment_body_prefixes_the_marker() -> None:
    body = build_comment_body("# Review\n\nsome text")
    assert body.startswith(COMMENT_MARKER)
    assert "some text" in body


def test_assert_postable_passes_clean_text() -> None:
    assert_postable("A normal review citing acme-web/src/x.ts:2. No secrets.")


def test_assert_postable_rejects_a_secret() -> None:
    with pytest.raises(ValidationError):
        assert_postable("token sk-ant-api03-ABCDEFGHIJKLMNOPQRST is here")


# ---------------------------------------------------------------------------
# head-SHA re-read
# ---------------------------------------------------------------------------


def test_current_head_sha_reads_fresh(fake_gh_bin: FakeGh) -> None:
    fake_gh_bin.set_scenario(gh.scenario(meta=gh.pr_meta(head_sha=gh.sha("c"))))
    assert _poster(fake_gh_bin).current_head_sha() == gh.sha("c")


# ---------------------------------------------------------------------------
# idempotent upsert
# ---------------------------------------------------------------------------


def test_upsert_creates_when_no_marked_comment_exists(fake_gh_bin: FakeGh) -> None:
    fake_gh_bin.set_scenario(gh.scenario(comments=[]))
    body = build_comment_body("# Review\n\nbody")
    assert _poster(fake_gh_bin).upsert(body) == "created"

    record = fake_gh_bin.posted_record()
    assert record["method"] == "POST"
    assert record["path"].endswith("issues/7/comments")
    assert COMMENT_MARKER in json.loads(record["input"])["body"]


def test_upsert_updates_the_existing_marked_comment(fake_gh_bin: FakeGh) -> None:
    fake_gh_bin.set_scenario(
        gh.scenario(comments=[{"id": 55, "body": f"{COMMENT_MARKER}\n\nold review"}])
    )
    assert _poster(fake_gh_bin).upsert(build_comment_body("# New")) == "updated"

    record = fake_gh_bin.posted_record()
    assert record["method"] == "PATCH"
    assert record["path"].endswith("issues/comments/55")  # targeted the right comment


def test_upsert_ignores_comments_without_the_marker(fake_gh_bin: FakeGh) -> None:
    fake_gh_bin.set_scenario(
        gh.scenario(comments=[{"id": 1, "body": "a human review, unrelated"}])
    )
    assert _poster(fake_gh_bin).upsert(build_comment_body("# R")) == "created"


def test_upsert_failure_raises_without_leaking_stderr(fake_gh_bin: FakeGh) -> None:
    secret = "gho_LEAKED_SHOULD_NOT_APPEAR"
    fake_gh_bin.set_scenario(gh.scenario(comments_rc=1, stderr=f"boom {secret}\n"))
    with pytest.raises(IntakeError) as excinfo:
        _poster(fake_gh_bin).upsert(build_comment_body("# R"))
    assert secret not in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI --post wiring
# ---------------------------------------------------------------------------


def _github_pr():
    from panorama.intake import PullRequest

    return PullRequest(
        owner="acme",
        repo="acme-api",
        number=7,
        url="https://github.com/acme/acme-api/pull/7",
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_ref="main",
        head_ref="feature",
        title="t",
        body="",
        diff="diff --git a/x b/x\n",
    )


def _validated():
    from panorama.validation import ValidatedReview

    return ValidatedReview(
        verdict="comment", summary="No supported impact.", findings=[], discarded=[]
    )


def _wire_common(monkeypatch, pr, validated):
    from panorama import cli

    class _Source:
        def __init__(self, *a, **k):
            pass

        def load(self):
            return pr

    class _Provisioner:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        # V2.8: the provisioner records what it chose to clone, so the
        # report can state considered-versus-examined counts.
        selection = None

        def provision(self, _pr):
            return object()

    monkeypatch.setattr(cli, "GitHubPullRequestSource", _Source)
    monkeypatch.setattr(cli, "WorkspaceProvisioner", _Provisioner)
    monkeypatch.setattr(cli, "ClaudeRunner", lambda *a, **k: object())
    # Returns (validated, truncated, ranked_repos); the ranking feeds the
    # "Repositories examined" provenance section added in V2.7.
    monkeypatch.setattr(cli, "_run_pipeline", lambda *a, **k: (validated, False, []))


def test_cli_post_reports_created(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from panorama import cli

    pr = _github_pr()
    _wire_common(monkeypatch, pr, _validated())

    class _Poster:
        def __init__(self, *a, **k):
            pass

        def current_head_sha(self):
            return pr.head_sha  # unchanged since review

        def upsert(self, body):
            assert COMMENT_MARKER in body
            return "created"

    monkeypatch.setattr(cli, "PRCommentPoster", _Poster)

    result = CliRunner().invoke(cli.app, ["review", "acme/acme-api#7", "--post"])
    assert result.exit_code == 0, result.output
    assert "Posted review comment (created)" in result.output


def test_cli_post_aborts_when_pr_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from panorama import cli
    from panorama.errors import EXIT_VALIDATION_ERROR

    pr = _github_pr()
    _wire_common(monkeypatch, pr, _validated())

    class _MovedPoster:
        def __init__(self, *a, **k):
            pass

        def current_head_sha(self):
            return "f" * 40  # the branch advanced since the review

        def upsert(self, body):  # pragma: no cover - must not be reached
            raise AssertionError("must not post a stale review")

    monkeypatch.setattr(cli, "PRCommentPoster", _MovedPoster)

    result = CliRunner().invoke(cli.app, ["review", "acme/acme-api#7", "--post"])
    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "moved" in result.output.lower()


def test_cli_post_rejected_for_local() -> None:
    from typer.testing import CliRunner

    from panorama.cli import app

    result = CliRunner().invoke(
        app, ["review", "--local", "acme-api", "--head", "p1-rename", "--post"]
    )
    assert result.exit_code != 0
