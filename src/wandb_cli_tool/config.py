"""Persistent credentials (API key + entity), stored in the user config dir."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

CONFIG_DIR_ENV = "WANDB_CLI_TOOL_CONFIG_DIR"


@dataclass(frozen=True)
class Credentials:
    api_key: str
    entity: str | None = None


def config_path() -> Path:
    base = os.environ.get(CONFIG_DIR_ENV) or user_config_dir("wandb-cli-tool")
    return Path(base) / "config.json"


def load() -> Credentials | None:
    try:
        data = json.loads(config_path().read_text())
        api_key = data["api_key"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(api_key, str) or not api_key:
        return None
    entity = data.get("entity")
    return Credentials(api_key, entity if isinstance(entity, str) and entity else None)


def save(creds: Credentials) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"api_key": creds.api_key, "entity": creds.entity})
    # Owner-only permissions, set at creation so the key is never world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    os.chmod(path, 0o600)


def clear() -> None:
    config_path().unlink(missing_ok=True)
