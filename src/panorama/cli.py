"""Panorama CLI entry point.

This module owns only the top-level `app` object and stub wiring. Other
agents attach the real subcommands (`review`, `fixtures`, `demo`, ...) here.
"""

from __future__ import annotations

from pathlib import Path

import typer

from panorama import __version__
from panorama.doctor import render_report, run_doctor
from panorama.errors import EXIT_GENERAL_ERROR, PanoramaError
from panorama.fixtures import bootstrap as bootstrap_fixtures
from panorama.intake import LocalPullRequestSource, PullRequest, resolve_local_repo

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


def _render_intake_summary(pr: PullRequest) -> str:
    """A short, safe summary of a normalized pull request.

    Deliberately does not print the diff body: even for local fixtures we keep
    the discipline of not dumping repository source to stdout. `--json` is the
    way to see the full normalized bundle.
    """
    files_changed = sum(1 for line in pr.diff.splitlines() if line.startswith("diff --git"))
    diff_lines = pr.diff.count("\n")
    return "\n".join(
        [
            "Local pull request",
            f"  repo:   {pr.owner}/{pr.repo}",
            f"  base:   {pr.base_ref} ({pr.base_sha[:7]})",
            f"  head:   {pr.head_ref} ({pr.head_sha[:7]})",
            f"  title:  {pr.title}",
            f"  diff:   {files_changed} file(s) changed, {diff_lines} diff line(s)",
            "",
            "Intake only: the review pipeline is not wired up yet (lands in S5).",
            "Use --json to see the full normalized pull request.",
        ]
    )


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
        help="Emit the normalized pull request as JSON instead of a summary.",
    ),
) -> None:
    """Review a pull request. This build wires up local intake (`--local`) only."""
    if local is not None and target is not None:
        raise typer.BadParameter("pass either --local or a GitHub PR reference, not both.")
    if local is None:
        # GitHub intake is a below-the-cut-line milestone; fail honestly.
        typer.echo(
            "error: GitHub PR intake is not implemented yet; "
            "use --local <repo> --head <branch>.",
            err=True,
        )
        raise typer.Exit(EXIT_GENERAL_ERROR)
    if head is None:
        raise typer.BadParameter("--head is required with --local.")

    try:
        repo_path = resolve_local_repo(local)
        pull_request = LocalPullRequestSource(repo_path, base, head).load()
    except PanoramaError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(exc.exit_code) from exc

    if json_output:
        typer.echo(pull_request.model_dump_json(indent=2))
    else:
        typer.echo(_render_intake_summary(pull_request))


# --- Extension point -------------------------------------------------------
# Other agents attach additional subcommands here, e.g.:
#     app.command()(demo)
# -----------------------------------------------------------------------------


if __name__ == "__main__":
    app()
