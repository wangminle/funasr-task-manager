"""Persistent CLI configuration stored at ~/.asr-cli.yaml."""

from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path.home() / ".asr-cli.yaml"

DEFAULTS: dict[str, Any] = {
    "server": "http://localhost:15797",
    "api_key": "",
    "output": "table",
}


def _load() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save(data: dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)


def get(key: str) -> Any:
    data = _load()
    return data.get(key, DEFAULTS.get(key))


def set_value(key: str, value: str) -> None:
    data = _load()
    data[key] = value
    _save(data)


def get_all() -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(_load())
    return merged
