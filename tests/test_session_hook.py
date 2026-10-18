"""The cloud SessionStart hook (.claude/hooks/session-start.sh): a failed pip install must show."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "session-start.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or os.name == "nt", reason="needs a Unix bash")


def run_hook(tmp_path, pip_rc):
    """Run the hook with stand-in python, ldconfig and apt-get, so nothing is installed."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "python").write_text(f"#!/bin/sh\necho 'pip says hello'\nexit {pip_rc}\n")
    (bin_ / "ldconfig").write_text("#!/bin/sh\necho libEGL.so.1\n")   # Qt libraries already there
    for f in bin_.iterdir():
        f.chmod(0o755)
    env_file = tmp_path / "env"
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "CLAUDE_CODE_REMOTE": "true",
           "CLAUDE_PROJECT_DIR": str(tmp_path), "CLAUDE_ENV_FILE": str(env_file)}
    return subprocess.run([BASH, str(HOOK)], env=env, capture_output=True, text=True, timeout=30), env_file


def test_failed_pip_install_fails_the_hook(tmp_path):
    r, env_file = run_hook(tmp_path, 1)
    assert r.returncode != 0
    assert "pip install" in r.stderr and "pip says hello" in r.stderr
    assert not env_file.exists()


def test_working_pip_install_passes(tmp_path):
    r, env_file = run_hook(tmp_path, 0)
    assert r.returncode == 0, r.stderr
    assert "QT_QPA_PLATFORM=offscreen" in env_file.read_text()
