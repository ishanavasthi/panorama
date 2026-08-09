"""Tests for the co-change channel.

This channel ships disabled, and on the only corpus available it abstains
completely. That combination is dangerous: "it abstained" and "it is broken"
look identical from the outside, and a channel nobody can tell apart from a
no-op is a channel nobody should trust later.

So the tests here build organisations with *real* history — multiple commits,
several authors, shared ticket references — and prove the mechanism actually
fires when there is something to find. The abstention on the fixture corpus is
then a statement about the corpus rather than an untested claim about the code.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from panorama.channels import RetrievalChannel
from panorama.cochange import (
    CO_COMMIT,
    MIN_COMMITS,
    SHARED_REFERENCE,
    CoChangeChannel,
    build_coupling,
    claim_rank,
    read_history,
)
from panorama.intake import PullRequest
from panorama.workspace import Workspace

BASE_TIME = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)


def commit(repo: Path, message: str, *, author: str, when: datetime, index: int) -> None:
    (repo / f"f{index}.txt").write_text(f"{message}\n")
    stamp = when.isoformat()
    env_args = [
        "-c",
        f"user.name={author}",
        "-c",
        f"user.email={author.replace(' ', '.').lower()}@example.invalid",
        "-c",
        "commit.gpgsign=false",
    ]
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(repo), *env_args, "commit", "-m", message],
        capture_output=True,
        check=True,
        env={
            **_base_env(),
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        },
    )


def _base_env() -> dict[str, str]:
    import os

    return {**os.environ}


def make_repo(root: Path, name: str, commits: list[tuple[str, str, datetime]]) -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], capture_output=True, check=True)
    for index, (message, author, when) in enumerate(commits):
        commit(repo, message, author=author, when=when, index=index)
    return repo


def history(messages: list[str], author: str, start: datetime) -> list[tuple[str, str, datetime]]:
    return [(m, author, start + timedelta(days=i)) for i, m in enumerate(messages)]


def plain(n: int) -> list[str]:
    return [f"routine change {i}" for i in range(n)]


def pull_request(repo: str) -> PullRequest:
    return PullRequest(
        owner="o",
        repo=repo,
        number=1,
        url="https://example.invalid/1",
        base_sha="0" * 40,
        head_sha="1" * 40,
        base_ref="main",
        head_ref="branch",
        title="t",
        body="",
        diff="",
    )


# ---------------------------------------------------------------------------
# reading history
# ---------------------------------------------------------------------------


def test_history_is_read_with_author_and_time(tmp_path: Path) -> None:
    make_repo(tmp_path / "org", "one", history(plain(3), "Ada", BASE_TIME))
    commits = read_history(Workspace(tmp_path / "org").repo("one"))
    assert len(commits) == 3
    assert {c.author for c in commits} == {"Ada"}
    assert all(c.when.tzinfo is not None for c in commits)


def test_ticket_keys_are_read_from_subject_and_body(tmp_path: Path) -> None:
    make_repo(
        tmp_path / "org",
        "one",
        [("PROJ-42 do the thing", "Ada", BASE_TIME), ("unrelated", "Ada", BASE_TIME)],
    )
    commits = read_history(Workspace(tmp_path / "org").repo("one"))
    assert {t for c in commits for t in c.tickets} == {"PROJ-42"}


def test_an_issue_number_is_not_a_ticket_key(tmp_path: Path) -> None:
    """`#12` is repo-local on every forge, so treating it as shared would
    couple two repositories that merely both have a twelfth issue."""
    make_repo(tmp_path / "org", "one", [("fixes #12", "Ada", BASE_TIME)])
    commits = read_history(Workspace(tmp_path / "org").repo("one"))
    assert not {t for c in commits for t in c.tickets}


# ---------------------------------------------------------------------------
# the guards
# ---------------------------------------------------------------------------


def test_thin_history_is_excluded_and_explained(tmp_path: Path) -> None:
    """A prior from a handful of commits is a number, not evidence."""
    org = tmp_path / "org"
    make_repo(org, "one", history(plain(2), "Ada", BASE_TIME))
    make_repo(org, "two", history(plain(2), "Ada", BASE_TIME))

    coupling = build_coupling(Workspace(org))

    assert coupling.pairs == {}
    assert set(coupling.thin) == {"one", "two"}
    assert any("fewer than" in note for note in coupling.notes)


def test_a_temporal_signal_that_couples_everything_is_discarded(tmp_path: Path) -> None:
    """The guard that matters most, and the one the fixture corpus triggers.

    One author committing to every repository in one burst couples the entire
    organisation — which says nothing about which pairs are related, and would
    hand fusion a vote for every sibling on every review.
    """
    org = tmp_path / "org"
    for name in ("one", "two", "three", "four"):
        make_repo(org, name, history(plain(MIN_COMMITS + 1), "Ada", BASE_TIME))

    coupling = build_coupling(Workspace(org))

    assert coupling.pairs == {}
    assert any("non-discriminating" in note for note in coupling.notes)


def test_a_discriminating_temporal_signal_is_kept(tmp_path: Path) -> None:
    """Two repositories worked on together, two worked on months apart."""
    org = tmp_path / "org"
    make_repo(org, "one", history(plain(MIN_COMMITS + 1), "Ada", BASE_TIME))
    make_repo(org, "two", history(plain(MIN_COMMITS + 1), "Ada", BASE_TIME))
    far = BASE_TIME + timedelta(days=400)
    make_repo(org, "three", history(plain(MIN_COMMITS + 1), "Grace", far))
    make_repo(org, "four", history(plain(MIN_COMMITS + 1), "Alan", far + timedelta(days=400)))

    coupling = build_coupling(Workspace(org))

    assert coupling.claim_for("one", "two") == CO_COMMIT
    assert coupling.claim_for("one", "three") is None
    assert coupling.claim_for("three", "four") is None


def test_a_shared_ticket_couples_two_repositories(tmp_path: Path) -> None:
    """The stronger signal: somebody typed the same key in both, on purpose."""
    org = tmp_path / "org"
    far = BASE_TIME + timedelta(days=400)
    make_repo(org, "one", history([*plain(MIN_COMMITS), "PROJ-7 span"], "Ada", BASE_TIME))
    make_repo(org, "two", history([*plain(MIN_COMMITS), "PROJ-7 span"], "Grace", far))
    make_repo(org, "three", history(plain(MIN_COMMITS + 1), "Alan", far + timedelta(days=400)))

    coupling = build_coupling(Workspace(org))

    assert coupling.claim_for("one", "two") == SHARED_REFERENCE
    assert coupling.claim_for("one", "three") is None


def test_a_shared_reference_outranks_a_co_commit() -> None:
    assert claim_rank(SHARED_REFERENCE) < claim_rank(CO_COMMIT)


def test_one_usable_repository_cannot_produce_a_pair(tmp_path: Path) -> None:
    org = tmp_path / "org"
    make_repo(org, "one", history(plain(MIN_COMMITS + 1), "Ada", BASE_TIME))
    make_repo(org, "two", history(plain(1), "Ada", BASE_TIME))

    coupling = build_coupling(Workspace(org))

    assert coupling.pairs == {}
    assert any("not enough repositories" in note for note in coupling.notes)


# ---------------------------------------------------------------------------
# the channel
# ---------------------------------------------------------------------------


def test_the_channel_satisfies_the_protocol() -> None:
    assert isinstance(CoChangeChannel(), RetrievalChannel)
    assert CoChangeChannel().name == "cochange"


def test_the_channel_ranks_a_coupled_sibling(tmp_path: Path) -> None:
    org = tmp_path / "org"
    far = BASE_TIME + timedelta(days=400)
    make_repo(org, "one", history([*plain(MIN_COMMITS), "PROJ-7 span"], "Ada", BASE_TIME))
    make_repo(org, "two", history([*plain(MIN_COMMITS), "PROJ-7 span"], "Grace", far))
    make_repo(org, "three", history(plain(MIN_COMMITS + 1), "Alan", far + timedelta(days=400)))

    result = CoChangeChannel().rank(pull_request("one"), Workspace(org))

    assert [r.repo for r in result.ranked] == ["two"]
    assert "ticket" in result.ranked[0].justification


def test_the_channel_never_ranks_the_pull_requests_own_repository(tmp_path: Path) -> None:
    org = tmp_path / "org"
    far = BASE_TIME + timedelta(days=400)
    make_repo(org, "one", history([*plain(MIN_COMMITS), "PROJ-7 span"], "Ada", BASE_TIME))
    make_repo(org, "two", history([*plain(MIN_COMMITS), "PROJ-7 span"], "Grace", far))

    result = CoChangeChannel().rank(pull_request("one"), Workspace(org))
    assert "one" not in [r.repo for r in result.ranked]


def test_the_channel_produces_no_file_level_leads(tmp_path: Path) -> None:
    """History says which repositories move together, never where in them."""
    org = tmp_path / "org"
    far = BASE_TIME + timedelta(days=400)
    make_repo(org, "one", history([*plain(MIN_COMMITS), "PROJ-7 span"], "Ada", BASE_TIME))
    make_repo(org, "two", history([*plain(MIN_COMMITS), "PROJ-7 span"], "Grace", far))

    assert CoChangeChannel().rank(pull_request("one"), Workspace(org)).hits == ()


def test_the_channel_explains_its_silence(tmp_path: Path) -> None:
    """A silent channel that says nothing about *why* is indistinguishable from
    a broken one, which is the whole risk with an experimental channel."""
    org = tmp_path / "org"
    make_repo(org, "one", history(plain(1), "Ada", BASE_TIME))
    make_repo(org, "two", history(plain(1), "Ada", BASE_TIME))

    result = CoChangeChannel().rank(pull_request("one"), Workspace(org))
    assert result.ranked == ()
    assert result.notes


# ---------------------------------------------------------------------------
# how it is wired in
# ---------------------------------------------------------------------------


def test_the_channel_is_off_by_default() -> None:
    """An unmeasured channel enabled by default is a quality claim nobody
    checked."""
    from panorama.retrieval import active_channels

    assert "cochange" not in [c.name for c in active_channels()]


def test_the_channel_can_be_switched_on_explicitly() -> None:
    from panorama.retrieval import active_channels

    names = [c.name for c in active_channels(experimental=("cochange",))]
    assert "cochange" in names


def test_a_typo_in_the_channel_name_is_refused() -> None:
    """Silently enabling nothing would look exactly like enabling something
    that did not help — which is the one conclusion this must not fake."""
    from panorama.retrieval import active_channels

    with pytest.raises(ValueError, match="unknown experimental channel"):
        active_channels(experimental=("co-change",))


def test_it_abstains_on_the_fixture_corpus(tmp_path: Path) -> None:
    """The recorded verdict, as a test rather than a claim in a document.

    Every fixture repository has a single commit on its default branch, so the
    thin-history guard excludes all of them. This is a fact about the corpus,
    and it is why the channel ships off: there is nothing here to measure it
    against.
    """
    from panorama.fixtures import bootstrap as bootstrap_fixtures

    root = tmp_path / "demo-org"
    bootstrap_fixtures(dest_root=root)
    coupling = build_coupling(Workspace(root))

    assert coupling.pairs == {}
    assert len(coupling.thin) == 6
