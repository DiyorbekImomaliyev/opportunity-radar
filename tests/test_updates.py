"""Safe self-update (SPEC R-UPD-1), with real temporary git repositories."""

import subprocess
import sys

from eventparser.updates import self_update


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})


def make_repos(tmp_path):
    remote, dev, mine = tmp_path / "remote.git", tmp_path / "dev", tmp_path / "mine"
    git("init", "--bare", "-q", "-b", "main", str(remote), cwd=tmp_path)
    git("clone", "-q", str(remote), str(dev), cwd=tmp_path)
    (dev / "tests").mkdir()
    (dev / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    git("add", ".", cwd=dev)
    git("commit", "-q", "-m", "v1", cwd=dev)
    git("push", "-q", "origin", "main", cwd=dev)
    git("clone", "-q", str(remote), str(mine), cwd=tmp_path)
    return dev, mine


def push_change(dev, test_body):
    (dev / "tests" / "test_ok.py").write_text(test_body)
    git("commit", "-q", "-am", "v2", cwd=dev)
    git("push", "-q", "origin", "main", cwd=dev)


def head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def test_up_to_date(tmp_path):
    _, mine = make_repos(tmp_path)
    assert self_update(mine, sys.executable) == (False, "already up to date")


def test_installs_a_version_whose_tests_pass(tmp_path, no_real_bot_restart):
    dev, mine = make_repos(tmp_path)
    push_change(dev, "def test_ok():\n    assert 1 + 1 == 2\n")
    updated, message = self_update(mine, sys.executable)
    assert no_real_bot_restart == [True]  # the bot is restarted to run the new code
    assert updated and "all tests passed" in message and head(mine) == head(dev)


def test_refuses_a_version_whose_tests_fail(tmp_path):
    dev, mine = make_repos(tmp_path)
    before = head(mine)
    push_change(dev, "def test_ok():\n    assert False\n")
    updated, message = self_update(mine, sys.executable)
    assert not updated and "NOT installed" in message and head(mine) == before
    worktrees = subprocess.run(["git", "worktree", "list"], cwd=mine, capture_output=True, text=True).stdout
    assert worktrees.count("\n") == 1  # the temporary worktree was cleaned up


def test_no_remote(tmp_path):
    git("init", "-q", str(tmp_path / "solo"), cwd=tmp_path)
    assert self_update(tmp_path / "solo", sys.executable)[1].startswith("no tracked remote")
