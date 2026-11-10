"""File-backed controls for the offline fake gh, never production settings."""
from __future__ import annotations

import json
import os
from pathlib import Path


def load(root: str | Path | None = None) -> dict:
    path = Path(root if root is not None else os.environ["GH_CONFIG_DIR"]) / "fakegh.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def configure(root: str | Path | None = None, **changes) -> None:
    directory = Path(root if root is not None else os.environ["GH_CONFIG_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    values = {**load(directory), **changes}
    temporary = directory / "fakegh.json.tmp"
    temporary.write_text(json.dumps(values), encoding="utf-8")
    temporary.replace(directory / "fakegh.json")
