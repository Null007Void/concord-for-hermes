"""Mirror the product plugin and the C1 probe fixture into the isolated Hermes home.

Idempotent: wipes and re-copies only ``isolated-home/plugins/concord-for-hermes``
and ``isolated-home/plugins/concord_probe``, and writes
``isolated-home/config.yaml``. Never touches any other home.

Two plugins are installed on purpose:

  * ``concord-for-hermes`` - the product (tool + CLI + skill).
  * ``concord_probe`` - the C1 measurement fixture, moved out of the product
    surface when the product tool was registered. It stays installable so the
    C1 evidence keeps being reproducible, not merely reported.

The isolated home's ``gh_command`` points at the process-level fake ``gh`` in
``tests/fakegh/``; that is test configuration, not a product default.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
ROOT = REPO.parent
HOME = ROOT / "isolated-home"
PRODUCT_SRC = REPO / "plugin"
PRODUCT_DEST = HOME / "plugins" / "concord-for-hermes"
PROBE_SRC = REPO / "tests" / "fixtures" / "concord_probe"
PROBE_DEST = HOME / "plugins" / "concord_probe"
FAKEGH = REPO / "tests" / "fakegh" / "gh.py"

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def _config() -> str:
    return f"""# Isolated HERMES_HOME for the Concord integration proof.
# The product plugin and the C1 probe fixture are both installed; the probe is a
# measurement fixture, not part of the product surface.
plugins:
  enabled:
    - concord_probe
    - concord-for-hermes
  # Shortened from the 30s default so the C1 timeout-worker test is bounded.
  hook_callback_timeout: 1.5
  entries:
    concord-for-hermes:
      settings:
        allowed_repositories:
          - octo/disposable
        gh_command:
          - {json.dumps(sys.executable)}
          - {json.dumps(str(FAKEGH))}
"""


def _install(src: pathlib.Path, dest: pathlib.Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=IGNORE)


def main() -> None:
    (HOME / "plugins").mkdir(parents=True, exist_ok=True)
    _install(PRODUCT_SRC, PRODUCT_DEST)
    _install(PROBE_SRC, PROBE_DEST)
    (HOME / "config.yaml").write_text(_config(), encoding="utf-8")
    print(json.dumps({
        "home": str(HOME),
        "product": str(PRODUCT_DEST),
        "product_files": sorted(p.name for p in PRODUCT_DEST.iterdir()),
        "probe": str(PROBE_DEST),
        "probe_files": sorted(p.name for p in PROBE_DEST.iterdir()),
        "gh_command": [sys.executable, str(FAKEGH)],
    }, indent=2))


if __name__ == "__main__":
    main()
