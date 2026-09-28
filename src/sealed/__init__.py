"""Sealed Pokemon TCG price tracker."""
from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sealed-tracker")
except PackageNotFoundError:  # a checkout that was never installed
    __version__ = "0.0.0"


def build_stamp() -> dict:
    """Which code produced a build: the release tag (D-36) and, in GitHub Actions, the commit."""
    return {"release": f"v{__version__}", "commit": os.environ.get("GITHUB_SHA")}
