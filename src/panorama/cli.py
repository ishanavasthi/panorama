"""Panorama CLI entry point.

This module owns only the top-level `app` object and stub wiring. Other
agents attach the real subcommands (`review`, `fixtures`, `demo`, ...) here.
"""

from __future__ import annotations

from pathlib import Path

import typer

from panorama import __version__
from panorama.claude_runner import ClaudeRunner
from panorama.delivery import (
    PRCommentPoster,
    assert_postable,
    build_comment_body,
)
from panorama.doctor import render_report, run_doctor
from panorama.errors import IntakeError, PanoramaError
from panorama.fixtures import bootstrap as bootstrap_fixtures
from panorama.intake import (
    GitHubPullRequestSource,
    LocalPullRequestSource,
    parse_pr_ref,
    resolve_local_repo,
)
from panorama.provision import WorkspaceProvisioner
from panorama.render import render_markdown, review_json_obj
from panorama.retrieval import retrieve
from panorama.review import run_review
from panorama.validation import validate_review
from panorama.workspace import Workspace

app = typer.Typer(
    name="panorama",
    help="Review a GitHub pull request using evidence from sibling repos.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the Panorama version and exit.",
    ),
) -> None:
    """Panorama: cross-repo PR review."""


@app.command()
def doctor(
    deep: bool = typer.Option(
        False,
        "--deep",
        "--full",
        help=(
            "Also run a live Claude CLI round trip that proves the sandbox "
            "boundary end to end. Costs one small model call."
        ),
    ),
) -> None:
    """Run preflight checks (claude CLI, gh auth, git, workspace perms).

    Exits 0 when every check passes (warnings do not fail the run), otherwise
    with the exit code of the first, most upstream failure.
    """
    # `run_doctor` never raises for a failing check and never exits, so the
    # exit-code decision lives here in the CLI layer and nowhere else.
    report = run_doctor(deep=deep)
    typer.echo(render_report(report))
    raise typer.Exit(report.exit_code)


fixtures_app = typer.Typer(
    name="fixtures",
    help="Manage the local mock organisation used for offline review.",
    no_args_is_help=True,
)
app.add_typer(fixtures_app)


@fixtures_app.command("bootstrap")
def fixtures_bootstrap(
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Rebuild target repositories even if they already exist and are "
            "not recognisably a prior bootstrap."
        ),
    ),
    dest: str | None = typer.Option(
        None,
        "--dest",
        help="Where to build the repositories (default: .panorama/demo-org).",
    ),
) -> None:
    """Build the four fixture repos as local git repositories with P1–P4 branches.

    Idempotent: re-running rebuilds each repository from the checked-in fixture
    data. Nothing here reaches the network, `gh`, or a Claude subscription.
    """
    dest_root = Path(dest) if dest is not None else None
    try:
        result = bootstrap_fixtures(dest_root=dest_root, force=force)
    except PanoramaError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(exc.exit_code) from exc

    typer.echo(f"Built {len(result.repos)} fixture repositories under {result.root}:")
    for repo in result.repos:
        # The main branch is always present; list the seeded branches after it.
        seeded = [b for b in repo.branches if b != "main"]
        suffix = f" (branches: {', '.join(seeded)})" if seeded else ""
        typer.echo(f"  {repo.name}{suffix}")


@app.command()
def review(
    target: str | None = typer.Argument(
        None,
        metavar="[PR]",
        help="owner/repo#n or PR URL. GitHub intake lands in a later milestone.",
    ),
    local: str | None = typer.Option(
        None,
        "--local",
        help="Review a local fixture repository instead of a GitHub PR.",
    ),
    base: str = typer.Option("main", "--base", help="Base ref (default: main)."),
    head: str | None = typer.Option(None, "--head", help="Head ref / branch to review."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the validated review as JSON instead of Markdown.",
    ),
    post: bool = typer.Option(
        False,
        "--post",
        help="Post the review as a single, idempotent comment on the GitHub PR.",
    ),
) -> None:
    """Review a pull request end to end (local fixture or GitHub PR).

    Pipeline: intake -> workspace -> deterministic retrieval -> Claude review
    -> host evidence validation -> reference-only rendering. Findings that fail
    validation are discarded, never downgraded; the report says how many.
    """
    if local is not None and target is not None:
        raise typer.BadParameter("pass either --local or a GitHub PR reference, not both.")
    if local is None and target is None:
        raise typer.BadParameter("pass a GitHub PR reference (owner/repo#n) or --local <repo>.")

    if local is not None:
        if head is None:
            raise typer.BadParameter("--head is required with --local.")
        if post:
            raise typer.BadParameter("--post needs a GitHub PR; it cannot post to a local fixture.")
        _review_local(local, base, head, json_output=json_output)
    else:
        _review_github(target, json_output=json_output, post=post)


def _run_pipeline(pull_request, workspace: Workspace, runner: ClaudeRunner):
    """Retrieval → review → host validation over a ready workspace."""
    retrieval = retrieve(pull_request, workspace)
    result = run_review(pull_request, workspace, retrieval, runner=runner)
    validated = validate_review(result.data, pull_request, workspace)
    return validated, retrieval.truncated


def _emit(pull_request, validated, truncated: bool, *, json_output: bool) -> None:
    if json_output:
        import json

        obj = review_json_obj(pull_request, validated, retrieval_truncated=truncated)
        typer.echo(json.dumps(obj, indent=2))
    else:
        typer.echo(render_markdown(pull_request, validated, retrieval_truncated=truncated))


def _review_local(local: str, base: str, head: str, *, json_output: bool) -> None:
    """Full pipeline over a local fixture repository."""
    try:
        repo_path = resolve_local_repo(local)
        pull_request = LocalPullRequestSource(repo_path, base, head).load()
        # The workspace is the organisation directory the repo lives in, so one
        # search spans every sibling. Grant exactly that root to the child.
        workspace = Workspace(repo_path.resolve().parent)
        runner = ClaudeRunner(allowed_workspace_roots=[workspace.root])
        validated, truncated = _run_pipeline(pull_request, workspace, runner)
    except PanoramaError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(exc.exit_code) from exc

    _emit(pull_request, validated, truncated, json_output=json_output)


def _review_github(target: str, *, json_output: bool, post: bool = False) -> None:
    """Full pipeline over a GitHub pull request.

    Intake normalizes the PR (S7); the provisioner clones the organisation's
    repositories into a locked workspace and checks the PR repo out at its head
    SHA (S8). The workspace then flows through the identical retrieval → review
    → validation → rendering path the local source uses. The clone workspace
    lives under ``~/.panorama/workspaces``, which is the runner's default
    allowed root, so no extra grant is needed. With ``--post`` the review is
    delivered as one idempotent PR comment (S9).
    """
    try:
        owner, repo, number = parse_pr_ref(target)
        pull_request = GitHubPullRequestSource(owner, repo, number).load()
        with WorkspaceProvisioner(owner) as provisioner:
            workspace = provisioner.provision(pull_request)
            runner = ClaudeRunner()
            validated, truncated = _run_pipeline(pull_request, workspace, runner)

        _emit(pull_request, validated, truncated, json_output=json_output)

        if post:
            _post_review(owner, repo, number, pull_request, validated, truncated)
    except PanoramaError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(exc.exit_code) from exc


def _post_review(owner, repo, number, pull_request, validated, truncated: bool) -> None:
    """Deliver the review as one idempotent PR comment, guarded against staleness.

    The head SHA is re-read immediately before posting and the post is aborted if
    the branch moved, and the assembled body passes a final secret screen so a
    review pinned to a superseded commit — or carrying a leaked credential —
    never reaches the thread.
    """
    poster = PRCommentPoster(owner, repo, number)

    current_head = poster.current_head_sha()
    if current_head != pull_request.head_sha:
        raise IntakeError(
            f"the pull request moved since it was reviewed "
            f"(reviewed {pull_request.head_sha[:12]}, now {current_head[:12]}). "
            "Nothing was posted; re-run the review."
        )

    markdown = render_markdown(pull_request, validated, retrieval_truncated=truncated)
    body = build_comment_body(markdown)
    assert_postable(body)
    action = poster.upsert(body)
    typer.echo(f"Posted review comment ({action}) on {owner}/{repo}#{number}.")


# --- Extension point -------------------------------------------------------
# Other agents attach additional subcommands here, e.g.:
#     app.command()(demo)
# -----------------------------------------------------------------------------


if __name__ == "__main__":
    app()
