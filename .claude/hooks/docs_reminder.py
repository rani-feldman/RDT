"""Stop hook: when this branch changed RDT's code but not CHANGELOG.md or the design document,
remind Claude once to update them (or to say why the change needs neither).

It reminds once per set of changed files, so a turn that decides no docs are needed is not
blocked again until more code changes.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

CODE = ("rdt/", "sim/")
DOCS = ("CHANGELOG.md", "docs/DESIGN_DOCUMENT.md")


def git(*args):
    out = subprocess.run(["git", *args], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def changed_files():
    base = git("merge-base", "HEAD", "origin/main").strip()
    files = set(git("diff", "--name-only", base).split()) if base else set()
    files |= set(git("diff", "--name-only", "HEAD").split())
    files |= set(git("ls-files", "--others", "--exclude-standard").split())
    return files


def main():
    event = json.load(sys.stdin)
    if event.get("stop_hook_active"):
        return
    files = changed_files()
    code = sorted(f for f in files if f.startswith(CODE) and f.endswith(".py"))
    if not code or any(f in files for f in DOCS):
        return
    git_dir = git("rev-parse", "--git-dir").strip()
    if not git_dir:
        return
    marker = Path(git_dir) / "claude-docs-reminder"
    key = hashlib.sha1("\n".join(code).encode()).hexdigest()
    if marker.exists() and marker.read_text() == key:
        return
    marker.write_text(key)
    print(json.dumps({
        "decision": "block",
        "reason": (
            "This branch changes RDT code (" + ", ".join(code[:5]) + (", ..." if len(code) > 5 else "")
            + ") but not CHANGELOG.md or docs/DESIGN_DOCUMENT.md. If it adds a feature or fixes a bug, "
            "add the CHANGELOG line and the design-document requirement and design section, and update "
            "USER_MANUAL.md, GUI.md or CLI.md where they describe the behaviour (see CLAUDE.md). "
            "If it is a refactor, test or tweak that needs none of these, say so and stop."
        ),
    }))


if __name__ == "__main__":
    main()
