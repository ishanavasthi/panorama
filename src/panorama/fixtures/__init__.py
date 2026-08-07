"""Local fixture organisation for Panorama.

The checked-in ``data/demo-org`` tree is plain source data describing a small,
intentionally coupled multi-repo organisation. ``bootstrap`` materialises it
into real git repositories under ``.panorama/demo-org/`` so the graded core
(retrieval + review) can run entirely offline, with no ``gh`` and no cloning.

This package contains no review logic. The only code here is the generic
git-materialisation in :mod:`panorama.fixtures.bootstrap`, which is driven
entirely by the layout of the data tree and knows nothing about specific repo
names, fields, or expected findings.
"""

from __future__ import annotations

from panorama.fixtures.bootstrap import BootstrapResult, RepoResult, bootstrap

__all__ = ["BootstrapResult", "RepoResult", "bootstrap"]
