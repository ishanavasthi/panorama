"""Labelled evaluation cases — the ground truth quality is measured against.

One TOML file per case under ``evals/cases/``. TOML rather than YAML because
``tomllib`` is in the standard library at this project's Python floor (3.11),
so the ground-truth format costs no dependency; these files are only ever read,
which is exactly what ``tomllib`` supports.

A case describes a pull request in the bootstrapped fixture organisation and
what a correct review of it looks like:

    id             = "field-rename-breaks-consumer"
    repo           = "..."          # the repository the PR is against
    base           = "main"
    head           = "..."          # the seeded branch under review
    expect_finding = true
    category       = "contract_break"
    target_repos   = ["..."]        # sibling(s) a correct finding must cite
    target_files   = ["repo/path"]  # optional, file-level ground truth
    description    = "..."

Negative controls set ``expect_finding = false`` and name no target. They are
the most important cases in the corpus and the easiest to score wrongly — see
``scoring.py`` for what offline scoring can and cannot say about them.

This module contains no fixture names: it defines the *shape* of ground truth,
never the ground truth itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from panorama.errors import ValidationError
from panorama.models import Category

#: Categories that assert something about *another* repository, and therefore
#: require a target repo in a positive case. ``single_repo`` deliberately does
#: not: it is a real finding with no cross-repository claim.
CROSS_REPO_CATEGORIES = frozenset(
    {"contract_break", "duplicate_logic", "convention", "cross_repo_conflict"}
)


class EvalCase(BaseModel):
    """One labelled pull request and the outcome a correct review produces."""

    # Unknown keys are a typo in hand-written ground truth, not an extension
    # point. Failing loudly beats silently scoring against a mislabelled case.
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable identifier, unique across the corpus.")
    repo: str = Field(description="Repository the pull request is against.")
    head: str = Field(description="Seeded branch under review.")
    base: str = Field(default="main", description="Base ref the branch diverged from.")

    expect_finding: bool = Field(
        description="Whether a correct review reports a finding at all."
    )
    category: Category | None = Field(
        default=None, description="Expected category; required for a positive case."
    )
    target_repos: list[str] = Field(
        default_factory=list,
        description="Sibling repositories a correct finding must cite.",
    )
    target_files: list[str] = Field(
        default_factory=list,
        description="Optional file-level ground truth, each as 'repo/path'.",
    )

    # -- negative-case controls ---------------------------------------------
    forbid_repos: list[str] = Field(
        default_factory=list,
        description="Repositories that must NOT surface in the top ranks.",
    )
    expect_no_hits: bool = Field(
        default=False,
        description="Assert retrieval surfaces no sibling hits at all.",
    )

    description: str = ""
    notes: str = ""

    _source: Path | None = PrivateAttr(default=None)

    @property
    def source(self) -> Path | None:
        """The file this case was loaded from, for error messages."""
        return self._source

    @property
    def is_cross_repo(self) -> bool:
        return self.category in CROSS_REPO_CATEGORIES

    @property
    def retrieval_scorable(self) -> bool:
        """Whether *offline* retrieval scoring can say anything about this case.

        A positive cross-repo case has a target repo to look for. A negative
        case is only retrieval-scorable if it carries an explicit expectation
        (``forbid_repos`` or ``expect_no_hits``) — retrieval always ranks
        *something*, so "no finding expected" on its own is a statement about
        the review, not about retrieval. Cases that fall through are reported
        as unscorable rather than silently counted as passes.
        """
        if self.expect_finding:
            return bool(self.target_repos)
        return bool(self.forbid_repos) or self.expect_no_hits

    @model_validator(mode="after")
    def _check_label_consistency(self) -> EvalCase:
        if not self.id.strip():
            raise ValueError("id must not be empty")

        if self.expect_finding:
            if self.category is None:
                raise ValueError("a positive case (expect_finding = true) needs a category")
            if self.is_cross_repo and not self.target_repos:
                raise ValueError(
                    f"category {self.category!r} asserts a cross-repository impact, "
                    "so target_repos must name at least one sibling"
                )
        else:
            if self.category is not None:
                raise ValueError(
                    "a negative case (expect_finding = false) must not set a category"
                )
            if self.target_repos:
                raise ValueError(
                    "a negative case must not set target_repos; use forbid_repos "
                    "to assert what must *not* surface"
                )

        if self.repo in self.target_repos:
            raise ValueError(
                f"target_repos names the pull request's own repository {self.repo!r}; "
                "cross-repository ground truth must point at a sibling"
            )

        for ref in self.target_files:
            if "/" not in ref.strip("/"):
                raise ValueError(
                    f"target_files entry {ref!r} must be 'repo/path', "
                    "so a hit can be matched to a repository"
                )
        return self


def split_file_ref(ref: str) -> tuple[str, str]:
    """Split a ``repo/path`` ground-truth reference into its two parts."""
    repo, _, path = ref.partition("/")
    return repo, path


def load_case_file(path: Path) -> EvalCase:
    """Load and validate one case file, naming the file in any error."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"could not read evaluation case {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"evaluation case {path} is not valid TOML: {exc}") from exc

    try:
        case = EvalCase(**raw)
    except Exception as exc:  # pydantic ValidationError or a validator's ValueError
        raise ValidationError(f"evaluation case {path} is not valid: {exc}") from exc

    case._source = path
    return case


def load_cases(root: Path, *, only: list[str] | None = None) -> list[EvalCase]:
    """Load every case under ``root``, sorted by id, with ids proven unique.

    ``only`` filters to specific case ids and errors on an id that does not
    exist — a typo'd ``--case`` silently scoring nothing would look like a pass.
    """
    root = Path(root)
    if not root.is_dir():
        raise ValidationError(
            f"evaluation case directory not found: {root}. "
            "Cases are checked in under evals/cases/."
        )

    cases = [load_case_file(path) for path in sorted(root.glob("*.toml"))]
    if not cases:
        raise ValidationError(f"no evaluation cases (*.toml) found under {root}")

    seen: dict[str, Path | None] = {}
    for case in cases:
        if case.id in seen:
            raise ValidationError(
                f"duplicate evaluation case id {case.id!r} in {case.source} "
                f"and {seen[case.id]}"
            )
        seen[case.id] = case.source

    if only:
        wanted = set(only)
        missing = sorted(wanted - seen.keys())
        if missing:
            raise ValidationError(
                f"unknown evaluation case id(s): {', '.join(missing)}. "
                f"Known ids: {', '.join(sorted(seen))}"
            )
        cases = [c for c in cases if c.id in wanted]

    cases.sort(key=lambda c: c.id)
    return cases
