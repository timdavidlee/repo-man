import csv

import pytest

from repo_man import github, review
from repo_man.cli import main


def repo(name, description="", topics=None, archived=False, pushed="2026-01-01T00:00:00Z"):
    return {
        "nameWithOwner": f"me/{name}",
        "description": description,
        "repositoryTopics": [{"name": t} for t in topics] if topics else None,
        "url": f"https://github.com/me/{name}",
        "isArchived": archived,
        "isFork": False,
        "isPrivate": False,
        "pushedAt": pushed,
    }


REPOS = [
    repo("bare"),
    repo("described", description="has one"),
    repo("complete", description="done", topics=["python"]),
    repo("old", archived=True),
]


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Run `repo-man review` against a fake GitHub, returning rows by repo name."""
    sheet = tmp_path / "out" / "review.csv"
    root = tmp_path / "root"
    root.mkdir()

    def _run(repos=REPOS, *extra, code=0):
        monkeypatch.setattr(github, "list_github_repos", lambda: repos)
        assert main(["review", str(root), "-o", str(sheet), *extra]) == code
        with sheet.open(newline="", encoding="utf-8-sig") as handle:
            return {r["repo"]: r for r in csv.DictReader(handle)}

    _run.sheet = sheet
    return _run


def edit(sheet, name, encoding="utf-8", **values):
    with sheet.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns, rows = list(reader.fieldnames), list(reader)
    columns += [k for k in values if k not in columns]
    for row in rows:
        if row["repo"] == name:
            row.update(values)
    with sheet.open("w", newline="", encoding=encoding) as handle:
        writer = csv.DictWriter(handle, columns, restval="")
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("which, expected", [
    ("either", {"me/bare", "me/described"}),
    ("description", {"me/bare"}),
    ("topics", {"me/bare", "me/described"}),
])
def test_filters(run, which, expected):
    assert set(run(REPOS, "--missing", which)) == expected


def test_columns_and_archived(run):
    rows = run(REPOS, "--archived")
    assert "me/old" in rows
    assert rows["me/described"]["missing_description"] == ""
    assert rows["me/described"]["missing_topics"] == "yes"
    assert rows["me/bare"]["teaching"] == ""


def test_marks_and_added_columns_survive_rerun(run):
    run()
    edit(run.sheet, "me/bare", encoding="utf-8-sig", teaching="yes", notes="a, \"b\"", mine="x")
    rows = run([repo("bare", pushed="2026-05-05T00:00:00Z"), repo("new")])
    bare = rows["me/bare"]
    assert (bare["teaching"], bare["notes"], bare["mine"]) == ("yes", 'a, "b"', "x")
    assert bare["last_push"] == "2026-05-05"
    assert rows["me/new"]["teaching"] == ""


def test_marked_rows_are_never_dropped(run):
    run()
    edit(run.sheet, "me/bare", teaching="no")
    edit(run.sheet, "me/described", teaching="yes")
    # described no longer matches the filter; bare is gone from GitHub.
    rows = run([repo("described", description="has one")], "--missing", "description")
    assert rows["me/bare"]["teaching"] == "no"
    assert rows["me/described"]["teaching"] == "yes"


@pytest.mark.parametrize("content", [
    "repo;teaching\nme/bare;yes\n",
    "name,teaching\nme/bare,yes\n",
    "repo,teaching\nme/bare,yes\nme/bare,no\n",
    "repo,teaching\n,yes\n",
    "repo,teaching\nme/bare,yes,stray\n",
])
def test_unrecognised_sheet_is_left_alone(run, tmp_path, content):
    run.sheet.parent.mkdir()
    run.sheet.write_text(content)
    assert main(["review", str(tmp_path / "root"), "-o", str(run.sheet)]) == 1
    assert run.sheet.read_text() == content


def test_github_failure_leaves_sheet_alone(run, monkeypatch, tmp_path):
    run()
    edit(run.sheet, "me/bare", teaching="yes")
    before = run.sheet.read_bytes()

    def boom():
        raise RuntimeError("offline")

    monkeypatch.setattr(github, "list_github_repos", boom)
    assert main(["review", str(tmp_path / "root"), "-o", str(run.sheet)]) == 1
    assert run.sheet.read_bytes() == before
