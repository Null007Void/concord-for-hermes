"""Offline release smoke checks. Not a substitute for pytest or archive proof."""
from __future__ import annotations

import ast
import json
import shutil
import tempfile
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseChecks(unittest.TestCase):
    def test_manifest_has_explicit_baseline(self):
        text = (ROOT / "plugin" / "plugin.yaml").read_text(encoding="utf-8-sig")
        # The authoritative YAML/runtime validation is Hermes plugins validate.
        lines = [line.strip() for line in text.splitlines()]
        self.assertEqual(lines.count('requires_hermes: ">=0.21.3"'), 1)
        self.assertIn("kind: standalone", lines)
        self.assertIn("capabilities: []", lines)
        self.assertIn("name: concord-for-hermes", lines)

    def test_install_payload_is_present(self):
        for filename in ("__init__.py", "plugin.yaml", "concord_kernel.py", "concord_tool.py",
                         "concord_gh_adapter.py", "concord_store.py", "concord_result.py",
                         "concord_backup.py", "concord_cli.py", "concord_status.py",
                         "skills/write-file/SKILL.md"):
            self.assertTrue((ROOT / "plugin" / filename).is_file(), filename)
        self.assertFalse((ROOT / "plugin.yaml").exists(), "Revisit catalog subdir for changed layout")
        self.assertTrue((ROOT / "tests" / "fakegh" / "gh.py").is_file())

    def test_release_python_parses(self):
        for name in ("release_demo.py", "hermes_recording_demo.py", "release_checks.py"):
            ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8-sig"), filename=name)

    def test_installer_accepts_download_folder_name(self):
        # A GitHub ZIP normally extracts to repo-main, not the maintainer's exact
        # folder name. Exercise the documented developer install in that layout.
        with tempfile.TemporaryDirectory(prefix="concord-install-portability-") as directory:
            checkout = Path(directory) / "concord-for-hermes-main"
            (checkout / "scripts").mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "install_plugin.py", checkout / "scripts")
            for relative in ("plugin", "tests/fixtures", "tests/fakegh"):
                shutil.copytree(ROOT / relative, checkout / relative,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            result = subprocess.run([sys.executable, str(checkout / "scripts" / "install_plugin.py")],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            installed = Path(summary["product"])
            self.assertEqual(installed.parent.parent, Path(directory) / "isolated-home")
            self.assertEqual((installed / "concord_tool.py").read_bytes(),
                             (ROOT / "plugin" / "concord_tool.py").read_bytes())
            self.assertEqual(summary["gh_command"][1], str(checkout / "tests" / "fakegh" / "gh.py"))

    def test_offline_demo_outcomes(self):
        completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "release_demo.py")],
                                   capture_output=True, text=True, encoding="utf-8", timeout=85)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for outcome in ("confirmed", "replayed", "conflict", "refused", "unknown"):
            self.assertIn(f'"outcome": "{outcome}"', completed.stdout)
        self.assertIn("exactly 2 fake writes total, no retry write", completed.stdout)
        self.assertIn("No live verification", completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
