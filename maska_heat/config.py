"""Loading of config.yaml (settings) and secrets.yaml (credentials).

Phase 0 keeps this as plain dicts; phase 1 replaces it with validated models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


def _load(path: Path, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"{path.name} not found — copy {path.stem}.example.yaml to {path.name} and fill it in")
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: Path | None = None) -> dict[str, Any]:
    return _load(path or ROOT / "config.yaml", required=True)


def load_secrets(path: Path | None = None) -> dict[str, Any]:
    return _load(path or ROOT / "secrets.yaml", required=False)


def require(section: dict[str, Any], *keys: str, where: str) -> list[Any]:
    """Return the values for keys, failing with a clear message if any is missing or empty."""
    missing = [k for k in keys if not section.get(k)]
    if missing:
        raise ConfigError(f"missing {', '.join(missing)} in {where}")
    return [section[k] for k in keys]
