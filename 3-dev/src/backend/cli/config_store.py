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
    """Get config value. Supports dot-notation for nested keys (e.g. 'notify.feishu_app_id')."""
    data = _load()
    if "." in key:
        parts = key.split(".", 1)
        section = data.get(parts[0])
        if isinstance(section, dict):
            return section.get(parts[1])
        return None
    return data.get(key, DEFAULTS.get(key))


def set_value(key: str, value: str) -> None:
    """Set config value. Supports dot-notation for nested keys (e.g. 'notify.feishu_app_id')."""
    data = _load()
    if "." in key:
        parts = key.split(".", 1)
        if parts[0] not in data or not isinstance(data[parts[0]], dict):
            data[parts[0]] = {}
        data[parts[0]][parts[1]] = value
    else:
        data[key] = value
    _save(data)


def get_all() -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(_load())
    return merged
