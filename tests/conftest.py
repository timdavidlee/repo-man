import pytest

from repo_man import tag


@pytest.fixture(autouse=True)
def no_github_writes(monkeypatch):
    """No test may change anything on GitHub. Returns the calls that were tried."""
    calls = []

    def blocked(repo, topic):
        calls.append((repo, topic))
        raise AssertionError("a test tried to write to GitHub")

    monkeypatch.setattr(tag, "add_topic", blocked)
    return calls
