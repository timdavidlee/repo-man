"""Build a CSV of GitHub repos with missing metadata, for you to mark up.

You fill in the `teaching` and `notes` columns. Everything else is refreshed from
GitHub on each run. Nothing here changes anything on GitHub.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

# Columns you edit; carried over between runs. Any column you add is kept too.
USER_COLUMNS = ["teaching", "notes"]
COLUMNS = [
    "repo", *USER_COLUMNS, "missing_description", "missing_topics", "description",
    "topics", "visibility", "fork", "archived", "last_push", "cloned", "url",
]
MISSING = ("either", "description", "topics")


def _yes(value: bool) -> str:
    return "yes" if value else ""


def to_row(repo: dict, cloned: set[str]) -> dict[str, str]:
    description = (repo.get("description") or "").strip()
    topics = [t["name"] for t in repo.get("repositoryTopics") or []]
    return {
        "repo": repo["nameWithOwner"],
        "teaching": "",
        "notes": "",
        "missing_description": _yes(not description),
        "missing_topics": _yes(not topics),
        "description": " ".join(description.split()),
        "topics": " ".join(topics),
        "visibility": "private" if repo["isPrivate"] else "public",
        "fork": _yes(repo["isFork"]),
        "archived": _yes(repo["isArchived"]),
        "last_push": (repo.get("pushedAt") or "")[:10],
        "cloned": _yes(repo["nameWithOwner"].lower() in cloned),
        "url": repo.get("url") or "",
    }


def is_missing(row: dict[str, str], which: str) -> bool:
    no_description = bool(row["missing_description"])
    no_topics = bool(row["missing_topics"])
    if which == "description":
        return no_description
    if which == "topics":
        return no_topics
    return no_description or no_topics


def read_sheet(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Existing (columns, rows keyed by lowercased repo name). Empty if no file.

    Raises ValueError rather than let an unrecognised file be overwritten.
    """
    if not path.exists():
        return [], {}
    # utf-8-sig: spreadsheet apps often add a byte order mark.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        if "repo" not in columns or "teaching" not in columns:
            raise ValueError(f"{path} has no 'repo' and 'teaching' columns; not overwriting it")
        rows: dict[str, dict[str, str]] = {}
        for record in reader:
            if None in record:
                raise ValueError(f"{path} has a row with too many cells; not overwriting it")
            row = {k: v or "" for k, v in record.items()}
            key = row["repo"].strip().lower()
            if key in rows or (not key and any(row.values())):
                raise ValueError(f"{path} has a duplicate or blank repo name; not overwriting it")
            if key:
                rows[key] = row
    return columns, rows


def merge(
    github_repos: list[dict],
    old_rows: dict[str, dict[str, str]],
    extra_columns: list[str],
    cloned: set[str],
    which: str = "either",
    archived: bool = False,
) -> list[dict[str, str]]:
    """Fresh rows for matching repos, plus every old row you have written in.

    A marked repo stays in the sheet even once it no longer matches the filter,
    or no longer shows up on GitHub.
    """
    keep_columns = USER_COLUMNS + extra_columns

    def marked(key: str) -> bool:
        return any(old_rows.get(key, {}).get(c, "").strip() for c in keep_columns)

    rows: dict[str, dict[str, str]] = {}
    for repo in github_repos:
        key = repo["nameWithOwner"].lower()
        row = to_row(repo, cloned)
        wanted = is_missing(row, which) and (archived or not repo["isArchived"])
        if wanted or marked(key):
            row.update({c: old_rows.get(key, {}).get(c, "") for c in keep_columns})
            rows[key] = row
    for key, old in old_rows.items():
        if key not in rows and marked(key):
            rows[key] = old
    # Most recently pushed first; ties by name.
    ordered = sorted(rows.values(), key=lambda r: r["repo"].lower())
    return sorted(ordered, key=lambda r: r.get("last_push", ""), reverse=True)


def write_sheet(path: Path, rows: list[dict[str, str]], extra_columns: list[str]) -> None:
    """Write via a temp file so a failure never leaves a half-written sheet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=COLUMNS + extra_columns, extrasaction="ignore", restval=""
        )
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)
