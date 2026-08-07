"""Deliver a finished review to a GitHub pull request as one comment.

Rendering to stdout and `--json` already exist; this module adds the optional
`--post`. Three properties matter, and all three live here:

- **Idempotent.** Panorama owns exactly one comment per pull request, tagged with
  a hidden marker. A second run *updates* that comment instead of stacking a new
  one, so re-reviewing a PR never litters the thread.
- **Never posts a stale review.** Between reading the PR and posting, the branch
  can move. The head SHA is re-checked immediately before posting and the post is
  aborted if it changed — a review pinned to a commit that is no longer the head
  would mislead.
- **Never leaks a secret into the thread.** The outgoing comment body passes a
  final secret-shape screen before it leaves the machine — defence in depth on
  top of the per-finding screening the validator already did.

`gh` owns the GitHub credential throughout; Panorama never handles a token, and
raw `gh` stderr is never surfaced.
"""

from __future__ import annotations

import json
import subprocess

from panorama.errors import IntakeError, PreflightError, ValidationError
from panorama.screening import contains_secret

#: Hidden HTML marker identifying the single comment Panorama owns on a PR. It
#: renders invisibly on GitHub and is how a later run finds the comment to update.
COMMENT_MARKER = "<!-- panorama:v1 -->"

#: GitHub returns at most this many comments per page; one page is enough for a
#: demo-scale thread. (Panorama's own comment is found by marker within it.)
_COMMENTS_PER_PAGE = 100


def build_comment_body(markdown: str) -> str:
    """Prefix the rendered review with the ownership marker."""
    return f"{COMMENT_MARKER}\n\n{markdown.strip()}\n"


def assert_postable(body: str) -> None:
    """Refuse to post a body that contains anything credential-shaped.

    The validator already screened each finding and the summary; this is the
    last gate on the fully assembled comment before it leaves the machine.
    """
    if contains_secret(body):
        raise ValidationError(
            "refusing to post: the assembled review comment contains a "
            "credential-shaped string. Nothing was posted."
        )


class PRCommentPoster:
    """Create or update Panorama's single comment on one pull request."""

    def __init__(self, owner: str, repo: str, number: int, *, gh_path: str = "gh") -> None:
        self.owner = owner
        self.repo = repo
        self.number = number
        self.gh_path = gh_path

    @property
    def _slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    # -- head-SHA guard -----------------------------------------------------

    def current_head_sha(self) -> str:
        """The pull request's current head SHA, read fresh from GitHub."""
        raw = self._run("api", f"repos/{self._slug}/pulls/{self.number}", what="re-read the PR")
        try:
            meta = json.loads(raw)
            return meta["head"]["sha"]
        except (ValueError, KeyError, TypeError) as exc:
            raise IntakeError(
                f"could not re-read the head commit of {self._slug}#{self.number} before posting."
            ) from exc

    # -- comment upsert -----------------------------------------------------

    def _existing_comment_id(self) -> int | None:
        raw = self._run(
            "api",
            f"repos/{self._slug}/issues/{self.number}/comments?per_page={_COMMENTS_PER_PAGE}",
            what="list existing PR comments",
        )
        try:
            comments = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(comments, list):
            return None
        for comment in comments:
            if isinstance(comment, dict) and COMMENT_MARKER in (comment.get("body") or ""):
                cid = comment.get("id")
                if isinstance(cid, int):
                    return cid
        return None

    def upsert(self, body: str) -> str:
        """Create Panorama's comment, or update it if one already exists.

        Returns ``"created"`` or ``"updated"``.
        """
        payload = json.dumps({"body": body})
        existing = self._existing_comment_id()
        if existing is None:
            self._run(
                "api",
                "-X",
                "POST",
                f"repos/{self._slug}/issues/{self.number}/comments",
                "--input",
                "-",
                input_text=payload,
                what="post the review comment",
            )
            return "created"
        self._run(
            "api",
            "-X",
            "PATCH",
            f"repos/{self._slug}/issues/comments/{existing}",
            "--input",
            "-",
            input_text=payload,
            what="update the review comment",
        )
        return "updated"

    # -- subprocess plumbing ------------------------------------------------

    def _run(self, *argv: str, what: str, input_text: str = "") -> str:
        try:
            proc = subprocess.run(
                [self.gh_path, *argv], input=input_text, capture_output=True, text=True
            )
        except FileNotFoundError as exc:
            raise PreflightError(
                "the GitHub CLI (`gh`) was not found on PATH; it is required to post a review."
            ) from exc
        if proc.returncode != 0:
            # No stderr echo: host-authored, redacted.
            raise IntakeError(
                f"failed to {what} on {self._slug}#{self.number} (gh exit {proc.returncode}). "
                "Check `gh auth status` and that you have write access to the repository."
            )
        return proc.stdout
