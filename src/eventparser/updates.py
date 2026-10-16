"""Safe self-update (SPEC R-UPD-1): only fast-forward to a version whose tests pass here."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import ROOT


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def self_update(repo: Path = ROOT, python: str = sys.executable) -> tuple[bool, str]:
    """(updated, message). Tests the new version in a separate worktree before applying it."""
    try:
        upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=repo)
    except subprocess.CalledProcessError:
        return False, "no tracked remote branch (push to GitHub first)"
    _git("fetch", "--quiet", cwd=repo)
    behind = int(_git("rev-list", "--count", "HEAD..@{u}", cwd=repo) or 0)
    if behind == 0:
        return False, "already up to date"
    target = _git("rev-parse", "--short", "@{u}", cwd=repo)
    with tempfile.TemporaryDirectory(prefix="radar-update-") as tmp:
        wt = Path(tmp) / "candidate"
        _git("worktree", "add", "--detach", str(wt), upstream, cwd=repo)
        try:
            test = subprocess.run(
                [python, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider"],
                cwd=wt, capture_output=True, text=True, env={"PYTHONPATH": str(wt / "src"), "PATH": "/usr/bin:/bin"},
                timeout=900,
            )
        finally:
            _git("worktree", "remove", "--force", str(wt), cwd=repo)
    if test.returncode != 0:
        tail = (test.stdout or test.stderr).strip().splitlines()[-1:] or ["tests failed"]
        return False, f"new version {target} NOT installed: its tests fail here ({tail[0][:150]})"
    try:
        _git("merge", "--ff-only", "--quiet", "@{u}", cwd=repo)
    except subprocess.CalledProcessError as e:
        return False, f"new version {target} passed tests but could not fast-forward: {e.stderr.strip()[:150]}"
    subprocess.run([python, "-m", "pip", "install", "-q", "-e", str(repo)], cwd=repo, capture_output=True)
    restart_bot()
    return True, f"updated to {target} ({behind} new commit{'s' if behind > 1 else ''}); all tests passed"


def restart_bot() -> None:
    """macOS: restart the bot agent so it runs the new code; elsewhere the service manager does it."""
    if shutil.which("launchctl"):
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/com.opportunityradar.bot"], capture_output=True)


def _uid() -> int:
    import os

    return os.getuid()
