"""Exercise the real subprocess boundary with dummy credentials only.

Also runnable without Hermes: PYTHONPATH=plugin python -m unittest discover
-s tests -p test_c14_gh_environment.py -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import concord_gh_adapter as A


# Independent expectations: changing the production allowlist cannot silently
# change what these regressions accept.
COMMON = (
    "PATH", "HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG", "LC_ALL",
)
WINDOWS = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP")
BLOCKED = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "HERMES_HOME",
    "GH_HOST", "GH_DEBUG", "PYTHONPATH", "LD_PRELOAD", "ALL_PROXY", "all_proxy",
    "FAKEGH_STATE", "FAKEGH_FIXTURE", "FAKEGH_VERSION", "ARBITRARY_WRAPPER_VAR",
)


class GhEnvironmentTests(unittest.TestCase):
    def test_posix_environment_is_an_exact_allowlist(self):
        parent = {key: "dummy-" + key for key in COMMON + WINDOWS + BLOCKED}
        with patch.object(A, "os", SimpleNamespace(name="posix", environ=parent)):
            self.assertEqual(A._gh_environment(), {key: parent[key] for key in COMMON})

    def test_windows_environment_includes_runtime_paths(self):
        parent = {key: "dummy-" + key for key in COMMON + WINDOWS + BLOCKED}
        with patch.object(A, "os", SimpleNamespace(name="nt", environ=parent)):
            self.assertEqual(A._gh_environment(), {key: parent[key] for key in COMMON + WINDOWS})

    def test_unset_variables_are_not_synthesized(self):
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform):
                with patch.object(A, "os", SimpleNamespace(name=platform, environ={})):
                    self.assertEqual(A._gh_environment(), {})

    def test_empty_values_are_preserved(self):
        with patch.object(A, "os", SimpleNamespace(name="posix", environ={"GH_TOKEN": "", "NO_PROXY": ""})):
            self.assertEqual(A._gh_environment(), {"GH_TOKEN": "", "NO_PROXY": ""})

    def test_each_environment_is_fresh_and_not_the_parent(self):
        parent = {"GH_TOKEN": "dummy-first"}
        with patch.object(A, "os", SimpleNamespace(name="posix", environ=parent)):
            first = A._gh_environment()
            parent["GH_TOKEN"] = "dummy-second"
            second = A._gh_environment()
            second["GH_TOKEN"] = "changed-child-copy"
            self.assertEqual(first, {"GH_TOKEN": "dummy-first"})
            self.assertEqual(parent, {"GH_TOKEN": "dummy-second"})

    def test_both_real_child_paths_receive_only_allowed_values(self):
        with tempfile.TemporaryDirectory(prefix="concord-gh-env-") as directory:
            root = Path(directory)
            probe = root / "probe.py"
            probe.write_text(
                "import json, os, pathlib, sys\n"
                "kind = 'version' if '--version' in sys.argv[2:] else 'api'\n"
                "pathlib.Path(sys.argv[1], kind + '.json').write_text(\n"
                "    json.dumps({'env': dict(os.environ), 'argv': sys.argv[2:]}), encoding='utf-8')\n"
                "print('gh version 2.40.1' if kind == 'version' else '{}')\n",
                encoding="utf-8",
            )
            allowed = COMMON + (WINDOWS if os.name == "nt" else ())
            parent = {key: "dummy-" + key for key in COMMON + WINDOWS + BLOCKED}
            parent.update(PATH=os.environ.get("PATH", ""), HOME=directory,
                          LANG="C", LC_ALL="C", TEMP=directory, TMP=directory)
            if os.name == "nt":
                parent["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            adapter = A.GithubContentsAdapter(
                allowed_repositories={("octo", "disposable")},
                gh_command=(sys.executable, "-I", str(probe), directory),
            )
            # Parent secrets are synthetic. No real credential is inspected or
            # written by the capture child, including on a failed assertion.
            with patch.dict(os.environ, parent, clear=True):
                expected = {(key.upper() if os.name == "nt" else key): os.environ[key]
                            for key in allowed if key in os.environ}
                self.assertEqual(adapter.ensure_ready(), (2, 40, 1))
                self.assertEqual(adapter._invoke(["api", "/probe"]).returncode, 0)
            for kind in ("version", "api"):
                with self.subTest(call_path=kind):
                    captured = json.loads((root / (kind + ".json")).read_text(encoding="utf-8"))
                    self.assertEqual(captured["env"], expected)
                    self.assertNotIn("OPENAI_API_KEY", captured["env"])
                    if kind == "api":
                        self.assertEqual(captured["argv"], ["api", "--hostname", "github.com", "/probe"])


if __name__ == "__main__":
    unittest.main()
