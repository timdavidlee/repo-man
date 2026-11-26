import subprocess

import pytest

from repo_man import github, tag
from repo_man.cli import main

real_add_topic = tag.add_topic  # captured before the autouse block replaces it


def repo(name, topics=(), private=False, archived=False):
    return {
        "nameWithOwner": f"me/{name}",
        "repositoryTopics": [{"name": t} for t in topics] or None,
        "isPrivate": private,
        "isArchived": archived,
    }


def rows(**marks):
    return {f"me/{n}": {"repo": f"me/{n}", "teaching": m} for n, m in marks.items()}


def planned(plan):
    return [(c.repo, c.topic) for c in plan.changes]


def test_yes_values_and_blank():
    live = [repo(n) for n in "abcd"]
    plan = tag.build_plan(rows(a="yes", b=" Y ", c="TRUE", d=""), live)
    assert planned(plan) == [("me/a", "teaching"), ("me/b", "teaching"), ("me/c", "teaching")]
    assert plan.blank == 1


def test_no_adds_nothing_unless_asked():
    plan = tag.build_plan(rows(a="no"), [repo("a")])
    assert (plan.changes, plan.no_without_topic) == ([], 1)
    plan = tag.build_plan(rows(a="no"), [repo("a")], no_topic="not-teaching")
    assert planned(plan) == [("me/a", "not-teaching")]


def test_skips_have_reasons():
    live = [repo("odd"), repo("old", archived=True), repo("flip", topics=["teaching"])]
    plan = tag.build_plan(
        rows(odd="maybe", old="yes", gone="yes", flip="no"), live, no_topic="not-teaching"
    )
    assert plan.changes == []
    reasons = dict(plan.skipped)
    assert "unrecognised" in reasons["me/odd"]
    assert "archived" in reasons["me/old"]
    assert "not in your GitHub" in reasons["me/gone"]
    assert "contradicts" in reasons["me/flip"]


def test_already_tagged_is_a_no_op():
    live = [repo("a", topics=["teaching", "python"]), repo("b", ["teaching"], archived=True)]
    plan = tag.build_plan(rows(a="yes", b="yes"), live)
    assert (plan.changes, plan.skipped, plan.already) == ([], [], 2)


@pytest.mark.parametrize("name", ["", "Teaching", "two words", "-lead", "a,b", "x" * 51, "a\n"])
def test_invalid_topic(name):
    with pytest.raises(ValueError):
        tag.build_plan({}, [], topic=name)


def test_topics_must_differ():
    with pytest.raises(ValueError):
        tag.build_plan({}, [], no_topic="teaching")


@pytest.fixture
def sheet(tmp_path, monkeypatch):
    path = tmp_path / "sheet.csv"
    path.write_text("repo,teaching\nme/a,yes\nme/b,yes\nme/c,yes\n")
    monkeypatch.setattr(
        github, "list_github_repos", lambda: [repo("a"), repo("b", private=True), repo("c")]
    )
    return path


def test_preview_never_writes(sheet, capsys, no_github_writes):
    before = sheet.read_bytes()
    assert main(["tag", str(sheet)]) == 0
    out = capsys.readouterr().out
    assert "3 topics to add (2 on public repos" in out
    assert "preview only" in out
    assert no_github_writes == []
    assert sheet.read_bytes() == before


def test_apply_continues_past_a_failure(sheet, capsys, monkeypatch):
    calls = []

    def fake(name, topic):
        calls.append((name, topic))
        if name == "me/b":
            raise RuntimeError("denied")

    monkeypatch.setattr(tag, "add_topic", fake)
    assert main(["tag", str(sheet), "--apply"]) == 1
    assert calls == [("me/a", "teaching"), ("me/b", "teaching"), ("me/c", "teaching")]
    captured = capsys.readouterr()
    assert "2 added, 1 failed" in captured.out
    assert "FAILED me/b: denied" in captured.err


def test_add_topic_command(monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    real_add_topic("me/a", "teaching")
    assert seen == [["gh", "repo", "edit", "me/a", "--add-topic", "teaching"]]


def test_missing_sheet(tmp_path):
    assert main(["tag", str(tmp_path / "none.csv")]) == 1
