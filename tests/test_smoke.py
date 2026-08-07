"""Smoke test: the package imports and exposes a version string."""

import panorama


def test_version_is_a_string() -> None:
    assert isinstance(panorama.__version__, str)
    assert panorama.__version__
