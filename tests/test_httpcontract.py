"""Tests for the HTTP-contract channel.

This channel exists to find couplings nothing declares, which means it has no
declaration to check itself against. That makes it the easiest channel to make
*look* like it works, and the tests are organised around the three ways it could
be quietly wrong rather than around its features:

1. **It becomes the lexical channel again.** If a token matches because a
   variable is spelled the same rather than because it travels over the wire,
   the channel adds a second vote for whatever lexical matching already found
   and improves the numbers while adding no information.
2. **It ranks the wrong kind of repository.** Defining an error code and sending
   one are different relationships. A shared library and a documentation
   repository must stay out, or the channel re-creates the exact confusion it
   was built to resolve.
3. **It speaks when it should be silent.** Every negative control in the corpus
   depends on a change that moves no contract producing no leads at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from panorama import httpcontract as http_mod
from panorama.cache import Cache
from panorama.channels import RetrievalChannel
from panorama.httpcontract import (
    ENDPOINT,
    PAYLOAD,
    HTTPContractChannel,
    RepoHTTPSurface,
    SurfaceIndexer,
    build_surface,
    claim_rank,
    contract_surface,
)
from panorama.intake import PullRequest
from panorama.workspace import Workspace


def make_org(root: Path, repos: dict[str, dict[str, str]]) -> Workspace:
    root.mkdir(parents=True, exist_ok=True)
    for name, files in repos.items():
        repo = root / name
        repo.mkdir(parents=True, exist_ok=True)
        # An empty repository cannot be committed, and several tests want one
        # purely as the pull request's own repository. The placeholder has no
        # extension any language pack claims, so it never reaches the surface.
        files = files or {".gitkeep": ""}
        for relative, text in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for args in (
            ["init", "-b", "main"],
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return Workspace(root)


def pull_request(repo: str, diff: str = "") -> PullRequest:
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
        diff=diff,
    )


def diff_for(path: str, removed: list[str], added: list[str]) -> str:
    body = "".join(f"-{line}\n" for line in removed) + "".join(
        f"+{line}\n" for line in added
    )
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n" + body
    )


# ---------------------------------------------------------------------------
# reading the contract surface out of a diff
# ---------------------------------------------------------------------------


def test_a_route_literal_yields_its_static_segments() -> None:
    surface = contract_surface(
        diff_for("src/server.ts", [], ['app.get("/reports/daily", handler);'])
    )
    assert surface.routes == {"reports", "daily"}


def test_interpolated_path_parameters_are_dropped() -> None:
    """Two spellings of one route have to agree, or nothing ever matches.

    A server writes `/widgets/:id`, a client writes `/widgets/${id}`, and a Go
    caller concatenates `"/widgets/"` with a variable. Only the static segments
    are common to all three, so only those are kept.
    """
    server = contract_surface(diff_for("a.ts", [], ['route("/widgets/:id")']))
    client = contract_surface(diff_for("b.ts", [], ["fetch(`/widgets/${id}`)"]))
    caller = contract_surface(diff_for("c.go", [], ['http.Get(base + "/widgets/")']))
    assert server.routes == client.routes == caller.routes == {"widgets"}


def test_a_python_f_string_prefix_is_not_mistaken_for_a_segment() -> None:
    surface = contract_surface(
        diff_for("c.py", [], ['requests.get(f"{API_ROOT}/reports")'])
    )
    assert surface.routes == {"reports"}


def test_an_import_path_is_not_a_route() -> None:
    """A module path is shaped exactly like a URL and means something else.

    Without this, deleting `import { x } from "./handlers/reports"` would claim
    the change moves an endpoint called `/handlers`.
    """
    surface = contract_surface(
        diff_for("src/server.ts", ['import { x } from "./handlers/reports";'], [])
    )
    assert surface.routes == frozenset()


def test_payload_fields_come_from_keys_tags_and_subscripts() -> None:
    keys = contract_surface(diff_for("a.ts", [], ["  expires_on: null,"]))
    tags = contract_surface(diff_for("b.go", [], ['	Expires *string `json:"expires_on"`']))
    subs = contract_surface(diff_for("c.py", [], ['    x = payload["expires_on"]']))
    assert "expires_on" in keys.fields
    assert "expires_on" in tags.fields
    assert "expires_on" in subs.fields


def test_a_payload_value_is_a_wordless_lowercase_literal() -> None:
    surface = contract_surface(
        diff_for("a.ts", ['code: "not_ready"'], ['code: "withdrawn"'])
    )
    assert surface.values == {"not_ready", "withdrawn"}


def test_prose_and_urls_are_not_payload_values() -> None:
    surface = contract_surface(
        diff_for("a.ts", [], ['msg: "the thing is gone", base: "https://x.example"'])
    )
    assert surface.values == frozenset()


def test_a_status_code_needs_a_status_position() -> None:
    """A bare integer must not read as a status, or every diff matches every diff."""
    positioned = contract_surface(diff_for("a.ts", [], ["res.status(410).send()"]))
    bare = contract_surface(diff_for("b.ts", [], ["const retries = 410;"]))
    assert positioned.statuses == {410}
    assert bare.statuses == frozenset()


def test_a_token_on_both_sides_is_not_a_change() -> None:
    """A reformat re-emits every field it touches; without subtraction that
    reads as the whole payload changing at once."""
    surface = contract_surface(
        diff_for(
            "a.ts",
            ["  return { widget_id: id, widget_name: name };"],
            ["  return {", "    widget_id: id,", "    widget_name: name,", "  };"],
        )
    )
    assert surface.fields == frozenset()
    assert surface.empty


def test_comments_contribute_nothing() -> None:
    surface = contract_surface(
        diff_for("a.ts", [], ['// the /reports endpoint returns { widget_id: 1 }'])
    )
    assert surface.empty


def test_documents_and_manifests_contribute_nothing() -> None:
    """A documentation edit is not a contract change, however much it looks like
    one — which is what keeps the docs-only negative control at zero."""
    docs = contract_surface(
        diff_for("docs/api.md", [], ['`GET /reports` returns `{"widget_id": 1}`'])
    )
    manifest = contract_surface(diff_for("package.json", ['"x": "1.0.0"'], ['"x": "1.1.0"']))
    assert docs.empty
    assert manifest.empty


def test_an_empty_diff_yields_nothing() -> None:
    assert contract_surface("").empty


# ---------------------------------------------------------------------------
# reading a repository's HTTP surface
# ---------------------------------------------------------------------------

CALLER = {
    "svc/client.py": (
        "import requests\n"
        "\n"
        "def load(widget_id):\n"
        '    r = requests.get(f"{BASE}/widgets/{widget_id}")\n'
        '    return r.json()["widget_name"]\n'
    ),
}

DEFINER = {
    "src/codes.ts": 'export const CODES = { NOT_READY: "not_ready" };\n',
}


def test_a_repository_that_calls_an_api_participates(tmp_path: Path) -> None:
    org = make_org(tmp_path / "org", {"caller": CALLER})
    surface = build_surface(org.repo("caller"))
    assert surface.participates
    assert "widgets" in surface.routes
    assert "widget_name" in surface.fields


def test_a_repository_that_only_defines_constants_does_not_participate(
    tmp_path: Path,
) -> None:
    """Naming a contract is not consuming one.

    This is the distinction the whole channel rests on: a shared library that
    defines an error code is not broken when the code changes — whoever *sends*
    or *reads* it is. Lexical matching cannot tell those apart, and it is why
    two contract cases used to rank their target second.
    """
    org = make_org(tmp_path / "org", {"lib": DEFINER})
    surface = build_surface(org.repo("lib"))
    assert not surface.participates
    assert "not_ready" in surface.values  # it is seen, it is just not eligible


def test_a_repository_with_no_source_never_participates(tmp_path: Path) -> None:
    org = make_org(tmp_path / "org", {"docs": {"README.md": "GET /widgets\n"}})
    surface = build_surface(org.repo("docs"))
    assert not surface.participates
    assert surface.files_indexed == 0


def test_a_served_route_counts_as_participation(tmp_path: Path) -> None:
    org = make_org(
        tmp_path / "org",
        {"svc": {"src/app.ts": 'app.get("/widgets", handleWidgets);\n'}},
    )
    assert build_surface(org.repo("svc")).participates


def test_a_symbol_only_mentioned_in_a_comment_is_not_on_the_surface(
    tmp_path: Path,
) -> None:
    org = make_org(
        tmp_path / "org",
        {"svc": {"src/a.py": 'import requests\nrequests.get(BASE)\n# see /widgets\n'}},
    )
    assert "widgets" not in build_surface(org.repo("svc")).routes


def test_the_file_cap_is_enforced_and_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(http_mod, "MAX_INDEXED_FILES", 2)
    org = make_org(
        tmp_path / "org",
        {"svc": {f"src/f{n}.ts": "export const a = 1;\n" for n in range(5)}},
    )
    surface = build_surface(org.repo("svc"))
    assert surface.files_indexed == 2
    assert surface.truncated


# ---------------------------------------------------------------------------
# the channel
# ---------------------------------------------------------------------------


def test_an_endpoint_claim_outranks_a_payload_claim() -> None:
    assert claim_rank(ENDPOINT) < claim_rank(PAYLOAD)


def test_a_caller_of_a_removed_endpoint_is_ranked(tmp_path: Path) -> None:
    org = make_org(tmp_path / "org", {"api": {}, "caller": CALLER})
    diff = diff_for("src/server.ts", ['app.get("/widgets/:id", handle);'], [])
    result = HTTPContractChannel().rank(pull_request("api", diff), org)
    assert [entry.repo for entry in result.ranked] == ["caller"]
    assert result.ranked[0].rank == claim_rank(ENDPOINT)


def test_a_definer_of_a_changed_value_is_not_ranked_but_a_speaker_is(
    tmp_path: Path,
) -> None:
    """The measured point of the channel, as a single assertion.

    Both repositories contain the string `not_ready`. Only one of them talks to
    the service, and only that one is coupled to the change.
    """
    speaker = {
        "internal/client.go": (
            "package client\n"
            "\n"
            'const codeNotReady = "not_ready"\n'
            "\n"
            "func Fetch() { resp, _ := http.Get(base); _ = resp.StatusCode }\n"
        )
    }
    org = make_org(tmp_path / "org", {"api": {}, "lib": DEFINER, "gw": speaker})
    diff = diff_for("src/h.ts", ['code: "not_ready"'], ['code: "withdrawn"'])
    result = HTTPContractChannel().rank(pull_request("api", diff), org)

    assert [entry.repo for entry in result.ranked] == ["gw"]
    assert any("lib" in note for note in result.notes)


def test_the_channel_points_at_the_exact_line(tmp_path: Path) -> None:
    org = make_org(tmp_path / "org", {"api": {}, "caller": CALLER})
    diff = diff_for("src/server.ts", [], ['res.json({ widget_name: name });'])
    result = HTTPContractChannel().rank(pull_request("api", diff), org)
    assert [(h.repo, h.path, h.line) for h in result.hits] == [
        ("caller", "svc/client.py", 5)
    ]


def test_the_channel_is_silent_when_the_change_moves_no_contract(
    tmp_path: Path,
) -> None:
    """The property every negative control in the corpus depends on."""
    org = make_org(tmp_path / "org", {"api": {}, "caller": CALLER})
    diff = diff_for("src/util.ts", ["function helper() {}"], ["function assist() {}"])
    result = HTTPContractChannel().rank(pull_request("api", diff), org)
    assert result.ranked == ()
    assert result.hits == ()
    assert result.notes


def test_the_pull_requests_own_repository_is_never_ranked(tmp_path: Path) -> None:
    org = make_org(tmp_path / "org", {"caller": CALLER})
    diff = diff_for("src/server.ts", ['app.get("/widgets/:id", h);'], [])
    result = HTTPContractChannel().rank(pull_request("caller", diff), org)
    assert result.ranked == ()


def test_a_repository_outside_the_workspace_cannot_be_ranked(tmp_path: Path) -> None:
    org = make_org(tmp_path / "org", {"api": {}})
    diff = diff_for("src/server.ts", ['app.get("/widgets/:id", h);'], [])
    result = HTTPContractChannel().rank(pull_request("api", diff), org)
    assert result.ranked == ()


def test_the_channel_satisfies_the_protocol() -> None:
    assert isinstance(HTTPContractChannel(), RetrievalChannel)


def test_the_channel_is_deterministic(tmp_path: Path) -> None:
    org = make_org(tmp_path / "org", {"api": {}, "caller": CALLER, "other": CALLER})
    diff = diff_for("src/server.ts", ['app.get("/widgets/:id", h);'], [])
    pr = pull_request("api", diff)
    first = HTTPContractChannel().rank(pr, org)
    second = HTTPContractChannel().rank(pr, org)
    assert first == second


# ---------------------------------------------------------------------------
# the discrimination guard
# ---------------------------------------------------------------------------


def test_a_token_every_speaker_names_is_discarded(tmp_path: Path) -> None:
    """A ranking that puts every candidate in the same place is no ranking.

    The organisation's central resource appears in every client it has. Matching
    it identifies nobody, and letting it through promotes every HTTP-speaking
    repository equally — which is how a well-ranked case turns into a tie.
    """
    org = make_org(
        tmp_path / "org", {"api": {}, "a": CALLER, "b": CALLER, "c": CALLER}
    )
    diff = diff_for("src/server.ts", [], ['app.get("/widgets", listWidgets);'])
    result = HTTPContractChannel().rank(pull_request("api", diff), org)

    assert result.ranked == ()
    assert any("discriminate" in note for note in result.notes), result.notes


def test_a_discarded_token_says_so(tmp_path: Path) -> None:
    """"It found nothing" and "it discarded what it found" are different answers
    and look identical from outside."""
    org = make_org(tmp_path / "org", {"api": {}, "a": CALLER, "b": CALLER})
    diff = diff_for("src/server.ts", [], ['app.get("/widgets", listWidgets);'])
    notes = " ".join(HTTPContractChannel().rank(pull_request("api", diff), org).notes)
    assert "/widgets" in notes


def test_a_token_only_some_speakers_name_survives(tmp_path: Path) -> None:
    other = {"svc/x.py": 'import requests\nr = requests.get(f"{BASE}/gadgets")\n'}
    org = make_org(tmp_path / "org", {"api": {}, "a": CALLER, "b": other})
    diff = diff_for("src/server.ts", [], ['app.get("/widgets", listWidgets);'])
    result = HTTPContractChannel().rank(pull_request("api", diff), org)
    assert [entry.repo for entry in result.ranked] == ["a"]


def test_the_guard_does_not_fire_on_a_single_candidate(tmp_path: Path) -> None:
    """With one eligible repository there is nothing to discriminate *between*,
    and a match still separates it from every repository that speaks no HTTP."""
    org = make_org(tmp_path / "org", {"api": {}, "a": CALLER, "lib": DEFINER})
    diff = diff_for("src/server.ts", [], ['app.get("/widgets", listWidgets);'])
    result = HTTPContractChannel().rank(pull_request("api", diff), org)
    assert [entry.repo for entry in result.ranked] == ["a"]


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------


@pytest.fixture
def org(tmp_path: Path) -> Workspace:
    return make_org(tmp_path / "org", {"api": {}, "caller": CALLER})


def surface_diff() -> str:
    return diff_for("src/server.ts", ['app.get("/widgets/:id", h);'], [])


def test_a_second_run_is_served_from_the_cache(org: Workspace, tmp_path: Path) -> None:
    with Cache(tmp_path / "c" / "o.db") as cache:
        indexer = SurfaceIndexer(cache)
        indexer.surface_for(org.repo("caller"))
        assert (indexer.hits, indexer.misses) == (0, 1)
        warm = SurfaceIndexer(cache)
        warm.surface_for(org.repo("caller"))
        assert (warm.hits, warm.misses) == (1, 0)


def test_a_warm_cache_reads_no_files(org: Workspace, tmp_path: Path, monkeypatch) -> None:
    with Cache(tmp_path / "c" / "o.db") as cache:
        SurfaceIndexer(cache).surface_for(org.repo("caller"))

        def explode(*_args, **_kwargs):  # pragma: no cover - must not be called
            raise AssertionError("a warm surface must not touch the filesystem")

        monkeypatch.setattr(http_mod, "iter_source_files", explode)
        warm = SurfaceIndexer(cache).surface_for(org.repo("caller"))
    assert warm.participates


def test_a_moved_commit_invalidates_the_surface(org: Workspace, tmp_path: Path) -> None:
    with Cache(tmp_path / "c" / "o.db") as cache:
        SurfaceIndexer(cache).surface_for(org.repo("caller"))
        path = org.repo("caller").path
        (path / "svc" / "extra.py").write_text(
            'import requests\nr = requests.get(f"{BASE}/gadgets")\n'
        )
        for args in (
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "more"],
        ):
            subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True)

        indexer = SurfaceIndexer(cache)
        surface = indexer.surface_for(Workspace(path.parent).repo("caller"))
    assert (indexer.hits, indexer.misses) == (0, 1)
    assert "gadgets" in surface.routes


def test_a_corrupt_payload_reads_as_a_miss(org: Workspace, tmp_path: Path) -> None:
    view = org.repo("caller")
    with Cache(tmp_path / "c" / "o.db") as cache:
        cache.put_facts(view.name, view.head_sha, http_mod.CACHE_KIND, {"nonsense": True})
        indexer = SurfaceIndexer(cache)
        surface = indexer.surface_for(view)
    assert indexer.misses == 1
    assert surface.participates


def test_a_surface_round_trips_through_its_payload(org: Workspace) -> None:
    built = build_surface(org.repo("caller"))
    restored = RepoHTTPSurface.from_payload(
        built.repo, built.head_sha, built.to_payload()
    )
    assert restored == built


def test_a_payload_of_the_wrong_version_is_refused() -> None:
    assert RepoHTTPSurface.from_payload("r", "s", {"version": -1}) is None
    assert RepoHTTPSurface.from_payload("r", "s", ["not", "a", "mapping"]) is None


def test_using_a_cache_does_not_change_what_is_concluded(
    org: Workspace, tmp_path: Path
) -> None:
    """The cache is allowed to change how much work happens, never the answer."""
    pr = pull_request("api", surface_diff())
    cold = HTTPContractChannel().rank(pr, org)
    with Cache(tmp_path / "c" / "o.db") as cache:
        HTTPContractChannel(cache).rank(pr, org)  # warm it
        warm = HTTPContractChannel(cache).rank(pr, org)
    assert cold.ranked == warm.ranked
    assert cold.hits == warm.hits


def test_a_poisoned_cache_can_mislead_but_not_fabricate(
    org: Workspace, tmp_path: Path
) -> None:
    """A tampered surface may waste attention; it may not invent evidence.

    What it produces are places to look. Whether anything is *cited* is decided
    later against the live checkout, with no cache in that path.
    """
    view = org.repo("caller")
    with Cache(tmp_path / "c" / "o.db") as cache:
        cache.put_facts(
            view.name,
            view.head_sha,
            http_mod.CACHE_KIND,
            {
                "version": http_mod.INDEX_VERSION,
                "participates": True,
                "files_indexed": 1,
                "truncated": False,
                "routes": {"widgets": [["does/not/exist.py", 999]]},
                "fields": {},
                "values": {},
                "statuses": {},
            },
        )
        result = HTTPContractChannel(cache).rank(pull_request("api", surface_diff()), org)

    assert any(h.path == "does/not/exist.py" for h in result.hits)
    assert not (view.path / "does/not/exist.py").exists()
