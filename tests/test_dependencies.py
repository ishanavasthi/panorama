"""Tests for the dependency graph and its channel.

The failure mode this guards against is a **missing edge**, which is a silent
recall loss: nothing reports it, the channel simply says less than it should and
the review is quietly worse. So the awkward shapes get tests of their own — a
package whose declared name is nothing like its directory, a dependency that
lives outside the organisation, a manifest that will not parse, a repository
with no manifest at all, a cycle, and a monorepo root.

Real git repositories are built for each case rather than mocking the workspace,
because the thing being tested is *reading what is actually on disk*.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from panorama.channels import RetrievalChannel
from panorama.dependencies import (
    DEPENDENCY,
    DEPENDENT,
    DependencyChannel,
    build_graph,
    edge_rank,
)
from panorama.intake import PullRequest
from panorama.workspace import Workspace


def make_org(root: Path, repos: dict[str, dict[str, str]]) -> Workspace:
    """Build a workspace of real git repositories from ``{repo: {file: text}}``."""
    root.mkdir(parents=True, exist_ok=True)
    for name, files in repos.items():
        repo = root / name
        repo.mkdir(parents=True, exist_ok=True)
        for relative, text in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for args in (
            ["init", "-b", "main"],
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed"],
        ):
            subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, check=True
            )
    return Workspace(root)


def npm(name: str, deps: list[str] | None = None) -> str:
    body: dict = {"name": name}
    if deps:
        body["dependencies"] = {dep: "^1.0.0" for dep in deps}
    return json.dumps(body)


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


def ranked(workspace: Workspace, repo: str) -> dict[str, int]:
    result = DependencyChannel().rank(pull_request(repo), workspace)
    return {entry.repo: entry.rank for entry in result.ranked}


# ---------------------------------------------------------------------------
# resolving edges by declared name
# ---------------------------------------------------------------------------


def test_an_edge_resolves_between_two_repositories(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "client": {"package.json": npm("client", ["lib"])},
            "library": {"package.json": npm("lib")},
        },
    )
    graph = build_graph(workspace)
    assert graph.depends_on["client"] == {"library"}
    assert graph.depended_on_by["library"] == {"client"}


def test_the_declared_name_can_be_nothing_like_the_directory(tmp_path: Path) -> None:
    """The case that breaks directory-name matching, which is why it is not used.

    A repository directory and the package it publishes are routinely different
    things in a real organisation, and an implementation that matched dependency
    strings against folder names would find no edge here at all.
    """
    workspace = make_org(
        tmp_path / "org",
        {
            "api-server": {"package.json": npm("@scope/api", ["@scope/toolkit"])},
            "helpers-repo": {"package.json": npm("@scope/toolkit")},
        },
    )
    graph = build_graph(workspace)
    assert graph.depends_on["api-server"] == {"helpers-repo"}


def test_a_dependency_outside_the_workspace_is_recorded_not_dropped(
    tmp_path: Path,
) -> None:
    """"40 of 44 dependencies are external" is useful context, not noise."""
    workspace = make_org(
        tmp_path / "org", {"solo": {"package.json": npm("solo", ["express", "left-pad"])}}
    )
    graph = build_graph(workspace)
    assert graph.depends_on["solo"] == set()
    assert set(graph.external["solo"]) == {"express", "left-pad"}


def test_a_package_depending_on_its_own_name_is_not_an_edge(tmp_path: Path) -> None:
    workspace = make_org(tmp_path / "org", {"solo": {"package.json": npm("solo", ["solo"])}})
    graph = build_graph(workspace)
    assert graph.depends_on["solo"] == set()


def test_a_malformed_manifest_leaves_the_repository_edgeless(tmp_path: Path) -> None:
    """One unreadable manifest must not take the rest of the graph with it."""
    workspace = make_org(
        tmp_path / "org",
        {
            "broken": {"package.json": "{ this is not json"},
            "client": {"package.json": npm("client", ["lib"])},
            "library": {"package.json": npm("lib")},
        },
    )
    graph = build_graph(workspace)
    assert graph.nodes["broken"].declared_name is None
    assert graph.depends_on["broken"] == set()
    # The rest of the organisation still resolves.
    assert graph.depends_on["client"] == {"library"}


def test_a_repository_with_no_manifest_is_a_node_with_no_edges(tmp_path: Path) -> None:
    """A documents repository is an ordinary member of an organisation."""
    workspace = make_org(
        tmp_path / "org",
        {"docs": {"README.md": "# docs\n"}, "client": {"package.json": npm("client")}},
    )
    graph = build_graph(workspace)
    assert graph.nodes["docs"].declared_name is None
    assert graph.depends_on["docs"] == set()


def test_a_monorepo_style_manifest_creates_no_phantom_edges(tmp_path: Path) -> None:
    """Workspace globs are not package names and must not be read as edges."""
    manifest = json.dumps(
        {
            "name": "monorepo-root",
            "private": True,
            "workspaces": ["packages/*", "apps/*"],
            "dependencies": {"real-lib": "^1.0.0"},
        }
    )
    workspace = make_org(
        tmp_path / "org",
        {
            "monorepo": {"package.json": manifest},
            "library": {"package.json": npm("real-lib")},
        },
    )
    graph = build_graph(workspace)
    assert graph.depends_on["monorepo"] == {"library"}
    assert "packages/*" not in graph.external["monorepo"]


def test_a_duplicate_declared_name_resolves_deterministically(tmp_path: Path) -> None:
    """Two repositories claiming one package name is a real, if broken, state.

    Whatever is chosen must at least be the same on every run, or a ranking
    would change without anything changing.
    """
    workspace = make_org(
        tmp_path / "org",
        {
            "beta": {"package.json": npm("shared")},
            "alpha": {"package.json": npm("shared")},
            "client": {"package.json": npm("client", ["shared"])},
        },
    )
    first = build_graph(workspace).depends_on["client"]
    second = build_graph(workspace).depends_on["client"]
    assert first == second == {"alpha"}


def test_a_cycle_terminates(tmp_path: Path) -> None:
    """Two repositories depending on each other is legal and must not hang."""
    workspace = make_org(
        tmp_path / "org",
        {
            "one": {"package.json": npm("one", ["two"])},
            "two": {"package.json": npm("two", ["one"])},
        },
    )
    graph = build_graph(workspace)
    related = {repo for repo, _, _ in graph.related("one")}
    assert related == {"two"}


def test_a_longer_cycle_terminates(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "a": {"package.json": npm("a", ["b"])},
            "b": {"package.json": npm("b", ["c"])},
            "c": {"package.json": npm("c", ["a"])},
        },
    )
    assert {repo for repo, _, _ in build_graph(workspace).related("a")} == {"b", "c"}


def test_edges_resolve_across_languages_only_where_a_manifest_records_them(
    tmp_path: Path,
) -> None:
    """A Go module cannot require an npm package, and pretending otherwise would
    invent an edge. This is why the channel is silent for a third of the corpus."""
    workspace = make_org(
        tmp_path / "org",
        {
            "service": {"package.json": npm("service")},
            "gateway": {"go.mod": "module github.com/example/gateway\n\ngo 1.22\n"},
        },
    )
    graph = build_graph(workspace)
    assert graph.depends_on["gateway"] == set()
    assert graph.depended_on_by.get("service", set()) == set()


# ---------------------------------------------------------------------------
# direction and distance
# ---------------------------------------------------------------------------


def test_edge_rank_orders_direct_dependent_first() -> None:
    assert edge_rank(DEPENDENT, 1) == 1
    assert edge_rank(DEPENDENCY, 1) == 2
    assert edge_rank(DEPENDENT, 2) == 3
    assert edge_rank(DEPENDENCY, 2) == 4


def test_a_direct_dependent_outranks_a_transitive_one(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "core": {"package.json": npm("core")},
            "middle": {"package.json": npm("middle", ["core"])},
            "outer": {"package.json": npm("outer", ["middle"])},
        },
    )
    ranks = ranked(workspace, "core")
    assert ranks["middle"] < ranks["outer"]


def test_a_provider_never_takes_rank_one_even_when_alone(tmp_path: Path) -> None:
    """The property that keeps a rank meaning the same thing in every run.

    A consumer can be *broken* by this change; a provider can only be duplicated
    or contradicted by it. If ranks were assigned by comparing whatever this
    channel happened to find, a provider would be promoted to first place purely
    because nothing better turned up — and the rank would stop meaning "strong
    structural claim" and start meaning "best of a bad lot".
    """
    workspace = make_org(
        tmp_path / "org",
        {
            "client": {"package.json": npm("client", ["lib"])},
            "library": {"package.json": npm("lib")},
        },
    )
    ranks = ranked(workspace, "client")
    assert ranks == {"library": edge_rank(DEPENDENCY, 1)}
    assert ranks["library"] > 1


def test_a_consumer_does_take_rank_one(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "client": {"package.json": npm("client", ["lib"])},
            "library": {"package.json": npm("lib")},
        },
    )
    assert ranked(workspace, "library") == {"client": 1}


def test_a_repository_that_is_both_takes_the_stronger_relationship(
    tmp_path: Path,
) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "one": {"package.json": npm("one", ["two"])},
            "two": {"package.json": npm("two", ["one"])},
        },
    )
    # `two` both depends on `one` and is depended on by it. The dependent
    # relationship is the stronger claim and is the one reported.
    assert ranked(workspace, "one") == {"two": edge_rank(DEPENDENT, 1)}


def test_the_walk_is_bounded(tmp_path: Path) -> None:
    """Past a couple of hops "depends on" stops meaning anything actionable."""
    from panorama.dependencies import MAX_DISTANCE

    chain = {
        f"r{i}": {"package.json": npm(f"r{i}", [f"r{i - 1}"] if i else None)}
        for i in range(MAX_DISTANCE + 3)
    }
    workspace = make_org(tmp_path / "org", chain)
    ranks = ranked(workspace, "r0")
    assert len(ranks) == MAX_DISTANCE
    assert f"r{MAX_DISTANCE + 1}" not in ranks


# ---------------------------------------------------------------------------
# the channel
# ---------------------------------------------------------------------------


def test_the_channel_satisfies_the_protocol() -> None:
    assert isinstance(DependencyChannel(), RetrievalChannel)
    assert DependencyChannel().name == "dependency"


def test_the_channel_produces_no_file_level_leads(tmp_path: Path) -> None:
    """A manifest edge says *which* repository, never *where* in it.

    Emitting the manifest line as a lead would be technically true and
    practically misleading — and it would make every negative control in the
    corpus surface a lead for a change that touched nothing.
    """
    workspace = make_org(
        tmp_path / "org",
        {
            "client": {"package.json": npm("client", ["lib"])},
            "library": {"package.json": npm("lib")},
        },
    )
    assert DependencyChannel().rank(pull_request("client"), workspace).hits == ()


def test_the_channel_is_silent_when_no_edge_resolves(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "one": {"package.json": npm("one", ["external"])},
            "two": {"package.json": npm("two")},
        },
    )
    result = DependencyChannel().rank(pull_request("one"), workspace)
    assert result.ranked == ()
    assert any("no dependency edges" in note for note in result.notes)


def test_a_repository_that_declares_nothing_is_explained(tmp_path: Path) -> None:
    """The commonest reason this channel says nothing, and the least visible."""
    workspace = make_org(
        tmp_path / "org",
        {"docs": {"README.md": "# docs\n"}, "client": {"package.json": npm("client")}},
    )
    result = DependencyChannel().rank(pull_request("client"), workspace)
    assert any("declare no package name" in note for note in result.notes)


def test_a_pull_request_from_outside_the_workspace_says_nothing(tmp_path: Path) -> None:
    workspace = make_org(tmp_path / "org", {"client": {"package.json": npm("client")}})
    result = DependencyChannel().rank(pull_request("stranger"), workspace)
    assert result.ranked == ()
    assert any("not in the workspace" in note for note in result.notes)


def test_the_justification_names_the_declared_package(tmp_path: Path) -> None:
    """Provenance a reviewer can check: the package name, not the directory."""
    workspace = make_org(
        tmp_path / "org",
        {
            "api-server": {"package.json": npm("@scope/api", ["@scope/toolkit"])},
            "helpers-repo": {"package.json": npm("@scope/toolkit")},
        },
    )
    result = DependencyChannel().rank(pull_request("api-server"), workspace)
    assert "@scope/toolkit" in result.ranked[0].justification


def test_the_channel_ignores_the_diff_entirely(tmp_path: Path) -> None:
    """A standing structural fact, by design — and worth pinning, because it is
    also the channel's main cost: it votes identically for every pull request in
    a repository, including ones that change nothing consequential."""
    workspace = make_org(
        tmp_path / "org",
        {
            "client": {"package.json": npm("client", ["lib"])},
            "library": {"package.json": npm("lib")},
        },
    )
    quiet = pull_request("client")
    loud = pull_request("client")
    loud.diff = "diff --git a/x b/x\n+++ b/x\n+entirely unrelated content\n"

    assert [r.repo for r in DependencyChannel().rank(quiet, workspace).ranked] == [
        r.repo for r in DependencyChannel().rank(loud, workspace).ranked
    ]


@pytest.mark.parametrize("direction", [DEPENDENT, DEPENDENCY])
def test_edge_rank_is_stable_for_a_known_direction(direction: str) -> None:
    assert edge_rank(direction, 1) == edge_rank(direction, 1)
