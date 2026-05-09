"""Path helpers for CLI-exported artifacts."""

from __future__ import annotations

import os
from pathlib import Path


_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", ".git")


def detect_project_root() -> Path:
    """Detect repository root for local dev and container layouts.

    Walks up from this file's directory looking for common project markers.
    Falls back to ASR_PROJECT_ROOT env var or a sensible parent.
    """
    env_val = os.environ.get("ASR_PROJECT_ROOT")
    if env_val:
        return Path(env_val).resolve()
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if any((parent / marker).exists() for marker in _ROOT_MARKERS):
            return parent
    return current.parent


def get_default_download_dir() -> Path:
    """Default directory for CLI-downloaded result copies."""
    return detect_project_root() / "runtime" / "storage" / "downloads"