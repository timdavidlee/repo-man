"""Read-only comparison of local repos against your GitHub account, via `gh`."""

from __future__ import annotations

import json
import re
import subprocess

from .scan import RepoStatus

_SLUG = re.compile(
    r"^(?:https?://|ssh://(?:[^@/]+@)?|git://|[^@/:]+@)github\.com(?::\d+)?[:/]"
    r"([^/]+)/(.+?)(?:\.git)?/?$"
)


def slug(url: str) -> str | None:
    """'git@github.com:Owner/Repo.git' -> 'owner/repo'. None for non-GitHub URLs."""
    match = _SLUG.match(url.strip())
    return f"{match[1]}/{match[2]}".lower() if match else None


def list_github_repos(limit: int = 1000) -> list[dict]:
    """Repos owned by the account `gh` is logged in as."""
    proc = subprocess.run(
        ["gh", "repo", "list", "--limit", str(limit),
         "--json",
         "nameWithOwner,description,repositoryTopics,url,isArchived,isFork,isPrivate,pushedAt"],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh repo list failed")
    return json.loads(proc.stdout)


def compare(statuses: list[RepoStatus], remote_repos: list[dict]) -> dict:
    """Split repos into: not cloned locally, cloned, and local without a GitHub remote."""
    local: dict[str, str] = {}
    no_github: list[str] = []
    unknown: list[str] = []
    for status in statuses:
        if status.error and not status.remotes:
            unknown.append(status.name)
            continue
        slugs = [s for s in map(slug, status.remotes.values()) if s]
        if not slugs:
            no_github.append(status.name)
        for s in slugs:
            local[s] = status.name

    remote = {r["nameWithOwner"].lower(): r for r in remote_repos}
    return {
        "not_cloned": [remote[k] for k in sorted(remote.keys() - local.keys())],
        "cloned": sorted(remote.keys() & local.keys()),
        "local_without_github": sorted(no_github),
        # On GitHub, but not in your list: someone else's, or renamed/deleted.
        "local_other_owner": sorted(f"{local[k]} ({k})" for k in local.keys() - remote.keys()),
        "unreadable": sorted(unknown),
    }
