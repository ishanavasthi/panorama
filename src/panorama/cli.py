"""Panorama CLI entry point.

This module owns only the top-level `app` object and stub wiring. Other
agents attach the real subcommands (`review`, `fixtures`, `demo`, ...) here.
"""

from __future__ import annotations

from pathlib import Path

import typer

from panorama import __version__
from panorama.doctor import render_report, run_doctor
from panorama.errors import PanoramaError
from panorama.fixtures import bootstrap as bootstrap_fixtures

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


# --- Extension point -------------------------------------------------------
# Other agents attach additional subcommands here, e.g.:
#     app.command()(review)
#     app.command()(demo)
# -----------------------------------------------------------------------------


if __name__ == "__main__":
    app()
