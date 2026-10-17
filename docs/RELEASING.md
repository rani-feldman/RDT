# Making a release

A release is a GitHub release with the Windows installer, the portable zip and their checksums,
and its notes come from [CHANGELOG.md](../CHANGELOG.md). GitHub Actions builds it; you try it and
publish it.

## While developing: release notes

Every pull request that adds a feature or fixes a bug adds one line to `CHANGELOG.md`, under
`## Unreleased`, in `### New features` or `### Bug fixes`. Write it for someone using RDT, not for
a developer. Small changes (tweaks, refactors, documentation, tests) get no line.

## Make a release

1. Open the repository on GitHub, click **Actions**, then **release** on the left.
2. Click **Run workflow** (on the right), keep the branch **main**, type the version, such as
   `1.2.0`, and click the green **Run workflow**.
   - Fixes only: raise the last number (1.2.0 to 1.2.1). New features: the middle one
     (1.2.1 to 1.3.0). A change that old files or habits no longer work with: the first one.
3. The workflow:
   - renames `## Unreleased` in CHANGELOG.md to `## 1.2.0 (date)`, starts a new empty Unreleased,
     sets the version in `rdt/_version.py`, and commits that to main;
   - runs the tests, builds `RDT-Setup-1.2.0.exe` and `RDT-1.2.0-portable.zip`;
   - installs, upgrades and uninstalls the setup on a clean machine to check it (packaging/smoke_test.ps1);
   - pushes that commit to main and creates a **draft** release with the files and the notes. No
     git tag yet: GitHub creates `v1.2.0` when you publish the draft.
4. Click **Releases** on the repository's main page, open the draft, download the installer and
   try it on your bench.
5. If it is good, click **Edit** (the pencil), then **Publish release**. GitHub tags the commit
   `v1.2.0` at that moment, and from then on RDT tells its users the new version is out.

If the workflow fails, nothing is released and main is not changed (the commit is pushed only
after the build and the test pass). Fix the cause and run it again.

If a published release turns out bad, don't delete it: fix the bug and release the next patch
version. If the draft is bad, delete it (**Delete** on the draft): there is no tag to clean up.
Main keeps the release commit, so fix the bug and release the next patch version (1.2.1).

## By hand

- Pushing a tag `v1.2.0` also builds a draft release, once `rdt/_version.py` and CHANGELOG.md
  already say 1.2.0 (`python tools/release.py prepare 1.2.0`, commit, then `git tag v1.2.0`
  and `git push origin v1.2.0`).
- To build on your own PC (Python 3.11+ and [Inno Setup 6](https://jrsoftware.org/isdl.php)):
  `powershell -ExecutionPolicy Bypass -File tools\build_release.ps1`. The files are in `release\`.

## Signing

The installer and the programs are not signed yet, so Windows shows "Windows protected your PC" on
first run. When there is a code-signing certificate, set `RDT_SIGN_CERT` (the .pfx file) and
`RDT_SIGN_PASSWORD` for the build (repository secrets for Actions), and build_release.ps1 signs
every exe and the installer.

## What the installer does

Defined in [packaging/rdt.iss](../packaging/rdt.iss) (Inno Setup) and
[packaging/rdt.spec](../packaging/rdt.spec) (PyInstaller):

- Installs per user in `%LOCALAPPDATA%\Programs\RDT`, no admin rights. Python and every package
  are inside; nothing else is needed on the PC.
- A new setup closes RDT, uninstalls the old version silently, and installs; user files in
  `%LOCALAPPDATA%\rdt` are never touched. Installing an older version warns first.
- Optional `rdt` on the user's PATH (ticked), `.rdtw` association, Start menu shortcuts for RDT
  and the simulator, optional desktop shortcut.
- Uninstall removes the program, the PATH entry and the association, and asks whether to delete
  `%LOCALAPPDATA%\rdt` (No by default).
- The AppId in rdt.iss must never change: it is how a new setup finds the old one.
