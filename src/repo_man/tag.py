"""Turn the marks in the review sheet into GitHub topics.

Planning is read-only. `add_topic` is the only function in repo-man that
changes anything on GitHub, and it only ever adds a topic.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

YES = {"yes", "y", "true", "1"}
NO = {"no", "n", "false", "0"}
_TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")


@dataclass
class Change:
    repo: str
    topic: str
    private: bool


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (repo, reason)
    already: int = 0  # already have the topic
    blank: int = 0  # not marked yet
    no_without_topic: int = 0  # marked no, and "no" has no topic


def check_topic(name: str) -> str:
    if not _TOPIC.fullmatch(name):
        raise ValueError(
            f"invalid topic {name!r}: use lowercase letters, digits and hyphens, "
            "starting with a letter or digit, at most 50 characters"
        )
    return name


def build_plan(
    rows: dict[str, dict[str, str]],
    github_repos: list[dict],
    topic: str = "teaching",
    no_topic: str | None = None,
) -> Plan:
    """Compare the sheet's marks with the topics on GitHub right now."""
    check_topic(topic)
    if no_topic is not None and check_topic(no_topic) == topic:
        raise ValueError("--topic and --no-topic must be different")
    live = {r["nameWithOwner"].lower(): r for r in github_repos}
    plan = Plan()
    for key, row in rows.items():
        name = row["repo"].strip()
        mark = row.get("teaching", "").strip().lower()
        if not mark:
            plan.blank += 1
            continue
        if mark in YES:
            wanted, other = topic, no_topic
        elif mark in NO:
            if no_topic is None:
                plan.no_without_topic += 1
                continue
            wanted, other = no_topic, topic
        else:
            plan.skipped.append((name, f"unrecognised mark {row['teaching'].strip()!r}"))
            continue
        repo = live.get(key)
        if repo is None:
            plan.skipped.append((name, "not in your GitHub repo list"))
            continue
        current = {t["name"] for t in repo.get("repositoryTopics") or []}
        if other in current:
            plan.skipped.append(
                (name, f"has topic {other!r}, which contradicts the mark; remove it yourself")
            )
        elif wanted in current:
            plan.already += 1
        elif repo["isArchived"]:
            plan.skipped.append((name, "archived, so GitHub won't allow changes"))
        else:
            plan.changes.append(Change(repo["nameWithOwner"], wanted, repo["isPrivate"]))
    plan.changes.sort(key=lambda c: c.repo.lower())
    return plan


def add_topic(repo: str, topic: str) -> None:
    """Add one topic to one repo on GitHub. Raises RuntimeError on failure."""
    try:
        proc = subprocess.run(
            ["gh", "repo", "edit", repo, "--add-topic", topic],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh repo edit failed")
