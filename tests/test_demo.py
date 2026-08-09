"""Offline tests for live demo seeding (S10).

The seeder is the one component that writes to GitHub, so it is *never* run for
real here. Instead its command runner is injected: `bootstrap` builds the local
repositories with real git (offline), and every `gh`/`git` network operation is
recorded by a fake runner rather than executed. That proves the orchestration —
which repos are created, which branches pushed, which PRs opened, and the
confirmation gate — without a token or a network.
"""

from __future__ import annotations

import pytest

from panorama.demo import DemoError, DemoResult, DemoSeeder, SeededPR, demo_repo_names


class FakeRunner:
    """Records every command and answers the few the seeder inspects."""

    def __init__(self, *, existing: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.existing = existing or set()

    def __call__(self, argv, *, cwd=None, input_text: str = ""):
        argv = list(argv)
        self.calls.append(argv)
        tail = argv[1:]
        if tail[:2] == ["auth", "status"]:
            return (0, "")
        if tail[:2] == ["repo", "view"]:
            return (0, "") if argv[3] in self.existing else (1, "")
        if tail[:2] == ["pr", "create"]:
            return (0, "https://github.com/x/y/pull/1")
        return (0, "")

    def with_argv(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[1 : 1 + len(prefix)] == list(prefix)]


def _seeder(runner: FakeRunner, **kw) -> DemoSeeder:
    return DemoSeeder("testorg", run=runner, **kw)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_seed_creates_every_repo_and_opens_a_pr_per_seed_branch() -> None:
    runner = FakeRunner()
    result = _seeder(runner).seed()

    assert isinstance(result, DemoResult)
    assert set(result.repos) == set(demo_repo_names())

    # One private repo created per fixture repo, from the local checkout.
    creates = runner.with_argv("repo", "create")
    created_slugs = {c[3] for c in creates}
    assert created_slugs == {f"testorg/{name}" for name in demo_repo_names()}
    assert all("--private" in c for c in creates)

    # All branches pushed, and a PR opened for each seeded (non-main) branch.
    assert runner.with_argv("push", "origin", "--all") or [
        c for c in runner.calls if c[1:] and c[1] == "-C" and "--all" in c
    ]
    pr_calls = runner.with_argv("pr", "create")
    assert len(pr_calls) == len(result.prs) >= 4  # p1, p2, p3, p4 across the org


def test_pr_titles_come_from_the_fixture_data() -> None:
    runner = FakeRunner()
    _seeder(runner).seed()
    titles = []
    for call in runner.with_argv("pr", "create"):
        if "--title" in call:
            titles.append(call[call.index("--title") + 1])
    # Titles are real PR titles from the data tree, not branch names.
    assert any(len(t.split()) > 2 for t in titles)
    assert all(t for t in titles)


# ---------------------------------------------------------------------------
# preflight and clean-state handling
# ---------------------------------------------------------------------------


def test_seed_fails_when_gh_is_not_authenticated() -> None:
    class Unauthed(FakeRunner):
        def __call__(self, argv, *, cwd=None, input_text=""):
            if list(argv)[1:3] == ["auth", "status"]:
                self.calls.append(list(argv))
                return (1, "")
            return super().__call__(argv, cwd=cwd, input_text=input_text)

    with pytest.raises(DemoError, match="not authenticated"):
        _seeder(Unauthed()).seed()


def test_existing_repo_without_recreate_is_refused() -> None:
    # The conflict is placed on the *first* repository the seeder reaches. The
    # seeder creates repositories one at a time, so a conflict further down the
    # list legitimately leaves the earlier ones created — it refuses to clobber,
    # but it does not roll back. That is V1 demo behaviour, recorded here rather
    # than asserted away.
    first = demo_repo_names()[0]
    runner = FakeRunner(existing={f"testorg/{first}"})
    with pytest.raises(DemoError, match="already exists"):
        _seeder(runner).seed()
    assert not runner.with_argv("repo", "create")  # nothing was created


def test_recreate_deletes_before_creating() -> None:
    first = demo_repo_names()[0]
    runner = FakeRunner(existing={f"testorg/{first}"})
    _seeder(runner, recreate=True).seed()
    deletes = runner.with_argv("repo", "delete")
    assert any(c[3] == f"testorg/{first}" for c in deletes)


# ---------------------------------------------------------------------------
# CLI confirmation gate
# ---------------------------------------------------------------------------


class _RecordingSeeder:
    instances: list[_RecordingSeeder] = []

    def __init__(self, owner, **kw):
        self.owner = owner
        self.seeded = False
        _RecordingSeeder.instances.append(self)

    def seed(self) -> DemoResult:
        self.seeded = True
        return DemoResult(
            owner=self.owner,
            repos=["acme-api"],
            prs=[SeededPR(repo="acme-api", branch="p1-rename", url="https://x/pull/1")],
        )


@pytest.fixture(autouse=True)
def _reset_recording():
    _RecordingSeeder.instances.clear()
    yield


def test_cli_demo_declined_seeds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from panorama import cli

    monkeypatch.setattr(cli, "DemoSeeder", _RecordingSeeder)
    result = CliRunner().invoke(cli.app, ["demo", "--github", "testorg"], input="n\n")
    assert result.exit_code != 0  # aborted
    assert _RecordingSeeder.instances == []  # never even constructed a seeder run


def test_cli_demo_confirmed_seeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from panorama import cli

    monkeypatch.setattr(cli, "DemoSeeder", _RecordingSeeder)
    result = CliRunner().invoke(cli.app, ["demo", "--github", "testorg"], input="y\n")
    assert result.exit_code == 0, result.output
    assert _RecordingSeeder.instances[-1].seeded
    assert "Seeded" in result.output


def test_cli_demo_yes_skips_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from panorama import cli

    monkeypatch.setattr(cli, "DemoSeeder", _RecordingSeeder)
    result = CliRunner().invoke(cli.app, ["demo", "--github", "testorg", "--yes"])
    assert result.exit_code == 0, result.output
    assert _RecordingSeeder.instances[-1].seeded


# ---------------------------------------------------------------------------
# --update: refreshing an organisation that already exists
# ---------------------------------------------------------------------------


class ExistingOrg(FakeRunner):
    """Every repository already exists, and some branches already have PRs."""

    def __init__(self, *, open_branches=()) -> None:
        super().__init__(existing=set())
        self.open_branches = list(open_branches)

    def __call__(self, argv, *, cwd=None, input_text: str = ""):
        argv = list(argv)
        tail = argv[1:]
        if tail[:2] == ["repo", "view"]:
            self.calls.append(argv)
            return (0, "")  # everything exists
        if tail[:2] == ["pr", "list"]:
            self.calls.append(argv)
            import json as _json

            return (0, _json.dumps([{"headRefName": b} for b in self.open_branches]))
        return super().__call__(argv, cwd=cwd, input_text=input_text)


def test_update_refreshes_instead_of_creating() -> None:
    runner = ExistingOrg()
    result = _seeder(runner, update=True).seed()

    assert not runner.with_argv("repo", "create"), "nothing should be created"
    assert not runner.with_argv("repo", "delete"), "nothing should be deleted"
    assert set(result.updated) == set(demo_repo_names())

    forced = [c for c in runner.calls if "push" in c and "--force" in c]
    assert forced, "an existing repository has to be force-pushed"


def test_update_does_not_need_the_delete_scope() -> None:
    """The practical reason `--update` exists: an ordinary `repo` token cannot
    delete, so `--recreate` is unavailable to most people."""
    runner = ExistingOrg()
    _seeder(runner, update=True).seed()
    assert not runner.with_argv("repo", "delete")


def test_update_skips_branches_that_already_have_a_pull_request() -> None:
    """`gh pr create` fails on a branch that already has one open, and failing
    the whole run over that would make --update unusable exactly when it is
    most useful."""
    every_branch = [
        b
        for repo in demo_repo_names()
        for b in _branches_of(repo)
    ]
    runner = ExistingOrg(open_branches=every_branch)
    result = _seeder(runner, update=True).seed()

    assert result.prs == []
    assert not runner.with_argv("pr", "create")


def test_update_opens_a_pull_request_for_a_new_branch() -> None:
    runner = ExistingOrg(open_branches=["p1-rename"])
    result = _seeder(runner, update=True).seed()

    opened = {pr.branch for pr in result.prs}
    assert "p1-rename" not in opened
    assert opened, "the branches without a pull request should get one"


def test_without_update_an_existing_repository_is_still_refused() -> None:
    """The default stays conservative: creating repositories is the one thing
    here that re-running cannot undo."""
    runner = ExistingOrg()
    with pytest.raises(DemoError, match="--update"):
        _seeder(runner).seed()


def test_update_never_puts_a_token_in_a_remote_url() -> None:
    """`gh` owns the credential; the remote is a plain HTTPS URL and the
    credential helper supplies auth."""
    runner = ExistingOrg()
    _seeder(runner, update=True).seed()

    remotes = [c for c in runner.calls if "remote" in c and "add" in c]
    assert remotes
    for call in remotes:
        url = call[-1]
        assert url.startswith("https://github.com/")
        assert "@" not in url and "token" not in url.lower()


def _branches_of(repo: str) -> list[str]:
    from panorama.fixtures.bootstrap import DATA_ROOT

    branches = DATA_ROOT / repo / "branches"
    if not branches.is_dir():
        return []
    return sorted(p.name for p in branches.iterdir() if p.is_dir())
