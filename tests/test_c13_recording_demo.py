"""The recording demo must exercise installed Hermes, not only source handlers."""
from pathlib import Path
import subprocess
import sys


def test_installed_recording_rehearsal():
    demo = Path(__file__).resolve().parents[1] / "scripts" / "hermes_recording_demo.py"
    result = subprocess.run([sys.executable, str(demo)], capture_output=True, text=True,
                            encoding="utf-8", timeout=130)
    assert result.returncode == 0, result.stdout + result.stderr
    for outcome in ("confirmed", "replayed", "conflict", "refused", "unknown"):
        assert f'"outcome": "{outcome}"' in result.stdout
    assert "Installed copy loaded" in result.stdout
    assert "exactly 2 simulated writes" in result.stdout
    assert "LIVE TEST STILL REQUIRED" in result.stdout
