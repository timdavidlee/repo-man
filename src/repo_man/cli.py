"""Scan and manage a directory of local git repos."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import github, review, tag
from .scan import RepoStatus, age, scan


def default_root() -> Path:
    """$REPO_MAN_ROOT if set, otherwise the current directory."""
    return Path(os.environ.get("REPO_MAN_ROOT") or ".")


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip()
        for row in [header, *rows]
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _row(s: RepoStatus) -> list[str]:
    if s.error:
        return [s.name, "?", "", "", "", "", f"error: {s.error}"]
    branch = "(detached)" if s.detached else (s.branch or "?")
    changes = " ".join(
        f"{n}{mark}"
        for n, mark in ((s.modified, "M"), (s.untracked, "?"), (s.conflicts, "U"))
        if n
    )
    sync = " ".join(f"{mark}{n}" for n, mark in ((s.ahead, "↑"), (s.behind, "↓")) if n)
    # unpushed, behind and stash already have their own columns.
    notes = [f for f in s.flags if f not in ("unpushed", "behind", "stash")]
    return [s.name, branch, changes, sync, str(s.stashes or ""), age(s.last_commit), ", ".join(notes)]


def _resolve_root(path: Path) -> Path | None:
    root = path.expanduser().resolve()
    if root.is_dir():
        return root
    print(f"repo-man: not a directory: {root}", file=sys.stderr)
    return None


def cmd_scan(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if root is None:
        return 2
    statuses, others = scan(root, depth=args.depth, fetch=args.fetch, jobs=args.jobs)
    shown = [s for s in statuses if s.needs_attention] if args.attention else statuses

    if args.json:
        json.dump(
            {
                "root": str(root),
                "repos": [s.to_dict() for s in shown],
                "not_repos": [str(p) for p in others],
            },
            sys.stdout,
            indent=2,
        )
        print()
        return 0

    if shown:
        header = ["repo", "branch", "changes", "sync", "stash", "last", "notes"]
        print(_table([_row(s) for s in shown], header))
    dirty = sum(s.dirty for s in statuses)
    unpushed = sum(bool(s.ahead) for s in statuses)
    print(
        f"\n{len(statuses)} repos under {root}: "
        f"{dirty} with local changes, {unpushed} ahead of upstream"
    )
    if not args.fetch:
        print("sync counts are as of each repo's last fetch (use --fetch to refresh)")
    for s in statuses:
        if s.fetch_error:
            print(f"fetch failed for {s.name}: {s.fetch_error}")
    if others:
        print("not git repos: " + ", ".join(str(p.relative_to(root)) for p in others))
    return 0


def cmd_github(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if root is None:
        return 2
    statuses, _ = scan(root, depth=args.depth)
    try:
        result = github.compare(statuses, github.list_github_repos())
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"repo-man: {exc}", file=sys.stderr)
        return 1
    if not args.archived:
        result["not_cloned"] = [r for r in result["not_cloned"] if not r["isArchived"]]

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0

    def section(title: str, items: list[str]) -> None:
        print(f"\n{title} ({len(items)})")
        for item in items:
            print(f"  {item}")

    def describe(r: dict) -> str:
        labels = (("private", "isPrivate"), ("fork", "isFork"), ("archived", "isArchived"))
        tags = [label for label, key in labels if r[key]]
        pushed = (r.get("pushedAt") or "")[:10]
        return "  ".join([r["nameWithOwner"], pushed, *tags])

    section("On GitHub, not cloned here", [describe(r) for r in result["not_cloned"]])
    section("Local, no GitHub remote", result["local_without_github"])
    section("Local, GitHub remote that is not in your repo list", result["local_other_owner"])
    if result["unreadable"]:
        section("Could not be read", result["unreadable"])
    print(f"\n{len(result['cloned'])} of your GitHub repos are cloned under {root}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if root is None:
        return 2
    output = args.output.expanduser()
    try:
        columns, old_rows = review.read_sheet(output)
        remote = github.list_github_repos()
        statuses, _ = scan(root, depth=args.depth)
        cloned = set(github.compare(statuses, remote)["cloned"])
        extra = [c for c in columns if c not in review.COLUMNS]
        rows = review.merge(
            remote, old_rows, extra, cloned, which=args.missing, archived=args.archived
        )
        review.write_sheet(output, rows, extra)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"repo-man: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
        return 0
    no_description = sum(bool(r.get("missing_description")) for r in rows)
    no_topics = sum(bool(r.get("missing_topics")) for r in rows)
    marked = sum(bool(r.get("teaching", "").strip()) for r in rows)
    print(f"wrote {len(rows)} repos to {output}")
    print(f"{no_description} without a description, {no_topics} without topics")
    print(f"{marked} marked in the teaching column, {len(rows) - marked} still blank")
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    sheet = args.sheet.expanduser()
    try:
        if not sheet.exists():
            raise ValueError(f"no sheet at {sheet}; run `repo-man review` first")
        _, rows = review.read_sheet(sheet)
        plan = tag.build_plan(
            rows, github.list_github_repos(), topic=args.topic, no_topic=args.no_topic
        )
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"repo-man: {exc}", file=sys.stderr)
        return 1

    if plan.changes:
        table = [
            [c.repo, "private" if c.private else "PUBLIC", f"+{c.topic}"] for c in plan.changes
        ]
        print(_table(table, ["repo", "visibility", "topic"]))
    if plan.skipped:
        print(f"\nskipped ({len(plan.skipped)})")
        for name, reason in plan.skipped:
            print(f"  {name}: {reason}")
    public = sum(not c.private for c in plan.changes)
    print(
        f"\n{len(plan.changes)} topics to add ({public} on public repos, visible to anyone)"
        f"\n{plan.already} already tagged, {plan.no_without_topic} marked no with no topic to add, "
        f"{plan.blank} not marked"
    )

    if not args.apply:
        print("\npreview only: nothing was changed on GitHub")
        if plan.changes:
            print("run again with --apply to add these topics")
        return 0

    failed = 0
    print()
    for change in plan.changes:
        try:
            tag.add_topic(change.repo, change.topic)
            print(f"added {change.topic} to {change.repo}", flush=True)
        except RuntimeError as exc:
            failed += 1
            print(f"FAILED {change.repo}: {exc}", file=sys.stderr, flush=True)
    print(f"{len(plan.changes) - failed} added, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repo-man", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("root", nargs="?", type=Path, default=default_root(),
                       help="directory holding your repos (default: $REPO_MAN_ROOT or cwd)")
        p.add_argument("--depth", type=int, default=1,
                       help="how many levels to search (default: 1)")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    p_scan = sub.add_parser("scan", help="show the state of every repo")
    common(p_scan)
    p_scan.add_argument("--fetch", action="store_true",
                        help="git fetch each repo first (network, updates remote refs)")
    p_scan.add_argument("-a", "--attention", action="store_true",
                        help="only repos that need attention")
    p_scan.add_argument("-j", "--jobs", type=int, default=8, help="parallel scans (default: 8)")
    p_scan.set_defaults(func=cmd_scan)

    p_gh = sub.add_parser("github", help="compare local repos with your GitHub account (uses gh)")
    common(p_gh)
    p_gh.add_argument("--archived", action="store_true", help="include archived GitHub repos")
    p_gh.set_defaults(func=cmd_github)

    p_review = sub.add_parser(
        "review", help="write a CSV of GitHub repos missing a description or topics"
    )
    common(p_review)
    p_review.add_argument("-o", "--output", type=Path, default=Path("reports/teaching-review.csv"),
                          help="CSV to create or update (default: %(default)s)")
    p_review.add_argument("--missing", choices=review.MISSING, default="either",
                          help="which gap to list (default: %(default)s)")
    p_review.add_argument("--archived", action="store_true", help="include archived GitHub repos")
    p_review.set_defaults(func=cmd_review)

    p_tag = sub.add_parser(
        "tag", help="preview (or --apply) GitHub topics from your teaching marks"
    )
    p_tag.add_argument("sheet", nargs="?", type=Path, default=Path("reports/teaching-review.csv"),
                       help="sheet written by `review` (default: %(default)s)")
    p_tag.add_argument("--topic", default="teaching",
                       help="topic for repos marked yes (default: %(default)s)")
    p_tag.add_argument("--no-topic", help="topic for repos marked no (default: add nothing)")
    p_tag.add_argument("--apply", action="store_true",
                       help="add the topics on GitHub (default: preview only)")
    p_tag.set_defaults(func=cmd_tag)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
