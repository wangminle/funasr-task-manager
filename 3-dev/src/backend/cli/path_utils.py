"""Path helpers for CLI-exported artifacts."""

from __future__ import annotations

import os
from pathlib import Path


def detect_project_root() -> Path:
    """Detect repository root for local dev and container layouts."""
    env_val = os.environ.get("ASR_PROJECT_ROOT")
    if env_val:
        return Path(env_val).resolve()
    try:
        return Path(__file__).resolve().parents[4]
    except IndexError:
        return Path(__file__).resolve().parent.parent


def get_default_download_dir() -> Path:
    """Default directory for CLI-downloaded result copies."""
    return detect_project_root() / "runtime" / "storage" / "downloads"