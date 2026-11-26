"""Read-only scanning of local git repos.

Every git call here is a query, and GIT_OPTIONAL_LOCKS=0 stops `git status`
from rewriting the index. The one exception is `fetch=True`, which runs
`git fetch` and so updates remote-tracking refs.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

GIT_TIMEOUT = 20
FETCH_TIMEOUT = 60

# `git remote -v` line: name<TAB>url (fetch), possibly followed by a partial-clone filter.
_REMOTE = re.compile(r"^(?P<name>[^\t]+)\t(?P<url>.+?) \((?P<kind>fetch|push)\)(?: \[.*\])?$")
_URL_CREDENTIALS = re.compile(r"^(https?://)[^/@]+@")


@dataclass
class RepoStatus:
    name: str
    path: str
    branch: str | None = None  # None when HEAD is detached
    detached: bool = False
    empty: bool = False  # no commits yet
    upstream: str | None = None
    upstream_gone: bool = False  # upstream configured but ref is missing
    ahead: int = 0
    behind: int = 0
    modified: int = 0  # tracked files with staged or unstaged changes
    untracked: int = 0
    conflicts: int = 0
    stashes: int = 0
    remotes: dict[str, str] = field(default_factory=dict)
    last_commit: int | None = None  # unix seconds
    error: str | None = None  # the repo could not be scanned
    fetch_error: str | None = None  # scanned, but sync counts may be stale

    @property
    def dirty(self) -> bool:
        return bool(self.modified or self.untracked or self.conflicts)

    @property
    def flags(self) -> list[str]:
        """Short labels for everything besides local changes that may need attention."""
        if self.error:
            return ["error"]
        out = []
        if self.conflicts:
            out.append("conflicts")
        if self.detached:
            out.append("detached")
        if self.empty:
            out.append("empty")
        if not self.remotes:
            out.append("no-remote")
        elif self.upstream_gone:
            out.append("upstream-gone")
        elif not self.upstream and not self.detached:
            out.append("no-upstream")
        if self.ahead:
            out.append("unpushed")
        if self.behind:
            out.append("behind")
        if self.stashes:
            out.append("stash")
        if self.fetch_error:
            out.append("fetch-failed")
        return out

    @property
    def needs_attention(self) -> bool:
        return self.dirty or bool(self.flags)

    def to_dict(self) -> dict:
        return {**asdict(self), "dirty": self.dirty, "flags": self.flags}


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    """Run git in `repo` and return stdout. Raises RuntimeError on failure."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc.stdout


def is_repo(path: Path) -> bool:
    # .git is a directory in a normal clone and a file in a worktree/submodule.
    return (path / ".git").exists()


def find_repos(root: Path, depth: int = 1) -> tuple[list[Path], list[Path]]:
    """Return (repos, non-repo directories) found up to `depth` levels below root.

    Does not descend into a repo once found, and skips hidden directories.
    """
    repos: list[Path] = []
    others: list[Path] = []

    def walk(directory: Path, remaining: int) -> bool:
        """Collect from `directory`; return True if any repo was found below it."""
        try:
            children = sorted(
                p for p in directory.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        except OSError:
            return False
        found = False
        for child in children:
            if is_repo(child):
                repos.append(child)
                found = True
            elif remaining > 1 and not child.is_symlink() and walk(child, remaining - 1):
                found = True
            else:
                others.append(child)
        return found

    walk(root, depth)
    return repos, others


def parse_remotes(text: str) -> dict[str, str]:
    """Fetch URL for each remote, with any credentials in http(s) URLs removed."""
    remotes = {}
    for line in text.splitlines():
        match = _REMOTE.match(line)
        if match and match["kind"] == "fetch":
            remotes[match["name"]] = _URL_CREDENTIALS.sub(r"\1", match["url"])
    return remotes


def parse_status(text: str, status: RepoStatus) -> None:
    """Fill `status` from `git status --porcelain=v2 --branch` output."""
    saw_ab = False
    for line in text.splitlines():
        if line.startswith("# branch.oid "):
            status.empty = line.split(" ", 2)[2] == "(initial)"
        elif line.startswith("# branch.head "):
            head = line.split(" ", 2)[2]
            if head == "(detached)":
                status.detached = True
            else:
                status.branch = head
        elif line.startswith("# branch.upstream "):
            status.upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            saw_ab = True
            _, _, ahead, behind = line.split(" ")
            status.ahead = int(ahead)
            status.behind = abs(int(behind))
        elif line.startswith(("1 ", "2 ")):
            status.modified += 1
        elif line.startswith("u "):
            status.conflicts += 1
        elif line.startswith("? "):
            status.untracked += 1
    # git omits branch.ab when the upstream ref does not exist.
    status.upstream_gone = bool(status.upstream) and not saw_ab and not status.empty


def scan_repo(path: Path, fetch: bool = False) -> RepoStatus:
    status = RepoStatus(name=path.name, path=str(path))
    try:
        status.remotes = parse_remotes(_git(path, "remote", "-v"))
        if fetch and status.remotes:
            try:
                _git(path, "fetch", "--all", "--quiet", timeout=FETCH_TIMEOUT)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                status.fetch_error = _first_line(exc)
        parse_status(_git(path, "status", "--porcelain=v2", "--branch"), status)
        status.stashes = len(_git(path, "stash", "list").splitlines())
        if not status.empty:
            status.last_commit = int(_git(path, "log", "-1", "--format=%ct").strip())
    except subprocess.TimeoutExpired:
        status.error = "git timed out"
    except (RuntimeError, ValueError, OSError) as exc:
        status.error = _first_line(exc)
    return status


def scan(
    root: Path, depth: int = 1, fetch: bool = False, jobs: int = 8
) -> tuple[list[RepoStatus], list[Path]]:
    """Scan every repo under root. Returns (statuses, non-repo directories)."""
    repos, others = find_repos(root, depth)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        statuses = list(pool.map(lambda p: scan_repo(p, fetch=fetch), repos))
    return statuses, others


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def age(timestamp: int | None, now: float | None = None) -> str:
    """Compact relative age: 3h, 12d, 5mo, 2y."""
    if timestamp is None:
        return "-"
    now = time.time() if now is None else now
    seconds = max(0, int(now - timestamp))
    units = ((365 * 86400, "y"), (30 * 86400, "mo"), (86400, "d"), (3600, "h"), (60, "m"))
    for size, unit in units:
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return "now"
