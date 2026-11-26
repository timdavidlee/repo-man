import json
import os
import subprocess

import pytest

from repo_man import github
from repo_man.cli import main
from repo_man.scan import RepoStatus, age, find_repos, parse_remotes, scan, scan_repo


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    """Keep the user's git config (signing, hooks, default branch) out of tests."""
    config = tmp_path / "gitconfig"
    config.write_text("[user]\nname = Test\nemail = test@example.com\n[init]\ndefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def commit(repo, name="file.txt", text="one\n"):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"edit {name}")


def make_repo(path):
    path.mkdir(parents=True)
    git(path, "init")
    return path


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "root"
    path.mkdir()
    return path


@pytest.fixture
def clone(tmp_path, root):
    """A clone of a bare 'origin' living outside the scan root, in sync with it."""
    origin = tmp_path / "origin dir.git"  # space on purpose
    origin.mkdir()
    git(origin, "init", "--bare")
    seed = make_repo(tmp_path / "seed")
    commit(seed)
    git(seed, "push", str(origin), "main")
    git(root, "clone", str(origin), "clone")
    return root / "clone"


def test_clean_clone_needs_no_attention(clone):
    s = scan_repo(clone)
    assert (s.branch, s.upstream, s.ahead, s.behind) == ("main", "origin/main", 0, 0)
    assert "origin" in s.remotes
    assert s.last_commit
    assert not s.needs_attention


def test_dirty_untracked_and_stash(clone):
    (clone / "file.txt").write_text("stashed\n")
    git(clone, "stash")
    (clone / "file.txt").write_text("changed\n")
    (clone / "new.txt").write_text("new\n")
    s = scan_repo(clone)
    assert (s.modified, s.untracked, s.stashes) == (1, 1, 1)
    assert s.dirty
    assert s.flags == ["stash"]


def test_ahead_and_behind(tmp_path, clone):
    other = tmp_path / "other"
    git(tmp_path, "clone", str(tmp_path / "origin dir.git"), "other")
    commit(other, "theirs.txt")
    git(other, "push")
    commit(clone, "mine.txt")
    before = scan_repo(clone)
    assert (before.ahead, before.behind) == (1, 0)  # not fetched yet
    after = scan_repo(clone, fetch=True)
    assert (after.ahead, after.behind) == (1, 1)
    assert after.flags == ["unpushed", "behind"]


def test_failed_fetch_keeps_other_findings(tmp_path, clone):
    commit(clone, "mine.txt")
    git(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    s = scan_repo(clone, fetch=True)
    assert s.error is None
    assert s.fetch_error
    assert s.flags == ["unpushed", "fetch-failed"]


def test_upstream_gone(clone):
    git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    assert "upstream-gone" in scan_repo(clone).flags


def test_branch_without_upstream(clone):
    git(clone, "switch", "-c", "feature")
    assert scan_repo(clone).flags == ["no-upstream"]


def test_empty_no_remote_and_detached(root):
    empty = make_repo(root / "empty")
    assert scan_repo(empty).flags == ["empty", "no-remote"]
    detached = make_repo(root / "detached")
    commit(detached)
    git(detached, "checkout", "--detach")
    s = scan_repo(detached)
    assert s.branch is None
    assert s.flags == ["detached", "no-remote"]


def test_merge_conflict(root):
    repo = make_repo(root / "conflict")
    commit(repo)
    git(repo, "switch", "-c", "side")
    commit(repo, text="side\n")
    git(repo, "switch", "main")
    commit(repo, text="main\n")
    with pytest.raises(subprocess.CalledProcessError):
        git(repo, "merge", "side")
    s = scan_repo(repo)
    assert s.conflicts == 1
    assert "conflicts" in s.flags


def test_scan_does_not_rewrite_index(clone):
    # A stale timestamp with unchanged content makes `git status` want to
    # refresh the index.
    os.utime(clone / "file.txt", (1_000_000, 1_000_000))
    index = clone / ".git" / "index"
    before = index.read_bytes()
    scan_repo(clone)
    assert index.read_bytes() == before
    # Control: without the safeguard, the same state does rewrite it.
    git(clone, "status")
    assert index.read_bytes() != before


def test_broken_repo_does_not_stop_scan(root):
    commit(make_repo(root / "good"))
    (root / "broken").mkdir()
    (root / "broken" / ".git").write_text("gitdir: /nonexistent\n")
    statuses, _ = scan(root)
    by_name = {s.name: s for s in statuses}
    assert by_name["broken"].flags == ["error"]
    assert by_name["good"].error is None


def test_find_repos_depth(root):
    make_repo(root / "top")
    make_repo(root / "group" / "nested")
    (root / "plain").mkdir()
    (root / "loose.txt").write_text("")
    repos, others = find_repos(root)
    assert [p.name for p in repos] == ["top"]
    assert [p.name for p in others] == ["group", "plain"]
    repos, others = find_repos(root, depth=2)
    assert [p.name for p in repos] == ["nested", "top"]
    assert [p.name for p in others] == ["plain"]


def test_remote_credentials_are_stripped():
    text = "origin\thttps://user:secret@github.com/o/r.git (fetch) [blob:none]\n"
    assert parse_remotes(text) == {"origin": "https://github.com/o/r.git"}


@pytest.mark.parametrize("url", [
    "git@github.com:Owner/Repo.git",
    "https://github.com/Owner/Repo",
    "ssh://git@github.com/Owner/Repo.git",
])
def test_github_slug(url):
    assert github.slug(url) == "owner/repo"


@pytest.mark.parametrize("url", [
    "/tmp/x.git",
    "git@gitlab.com:o/r.git",
    "https://notgithub.com/o/r",
    "https://example.com/github.com/o/r",
])
def test_non_github_slug(url):
    assert github.slug(url) is None


def test_github_compare():
    statuses = [
        RepoStatus("a", "/a", remotes={"origin": "git@github.com:me/a.git"}),
        RepoStatus("local", "/local"),
        RepoStatus("theirs", "/theirs", remotes={"origin": "https://github.com/them/x"}),
        RepoStatus("bad", "/bad", error="boom"),
    ]
    remote = [{"nameWithOwner": "Me/A"}, {"nameWithOwner": "me/b"}]
    result = github.compare(statuses, remote)
    assert result["cloned"] == ["me/a"]
    assert result["not_cloned"] == [{"nameWithOwner": "me/b"}]
    assert result["local_without_github"] == ["local"]
    assert result["local_other_owner"] == ["theirs (them/x)"]
    assert result["unreadable"] == ["bad"]


def test_age():
    assert [age(t, now=100 * 86400) for t in (None, 100 * 86400, 99 * 86400, 0)] == [
        "-", "now", "1d", "3mo",
    ]


def test_cli_json_and_attention(clone, root, capsys):
    commit(make_repo(root / "local"))
    assert main(["scan", str(root), "--json", "--attention"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in data["repos"]] == ["local"]


def test_cli_table(clone, root, capsys):
    assert main(["scan", str(root)]) == 0
    assert "1 repos under" in capsys.readouterr().out


def test_cli_bad_root(tmp_path):
    assert main(["scan", str(tmp_path / "nope")]) == 2
