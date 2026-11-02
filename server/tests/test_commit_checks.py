"""Проверка коммитов: неоднозначный SHA, связь от непроверенного коммита."""
import subprocess

import pytest
from fastapi.testclient import TestClient

from pth.api import create_app
from pth.auth import issue_key


@pytest.fixture()
def env(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'c.db'}")
    with app.state.session_factory() as s:
        token = issue_key(s, name="admin", kind="admin")
        s.commit()
    c = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    c.post("/v1/projects", json={"key": "PTH", "name": "PTH"})
    return app, c, token


def _git(repo, *args, stdin=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True, check=True).stdout.strip()


def test_ambiguous_short_sha_is_reported_as_ambiguous(env, tmp_path):
    """API пропускает SHA от 7 символов, домен — любой префикс; проверяем домен на коротком общем префиксе двух коммитов."""
    from pth import domain
    app, c, _ = env
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    tree = _git(repo, "write-tree")
    seen, pair = {}, None
    for n in range(5000):  # коммиты, пока два не совпадут в первых 4 символах (обычно ~300)
        oid = _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit-tree", tree, "-m", str(n))
        if oid[:4] in seen:
            pair = (seen[oid[:4]], oid)
            break
        seen[oid[:4]] = oid
    assert pair, "не нашлось двух коммитов с общим префиксом"
    _git(repo, "update-ref", "refs/heads/main", pair[0])
    c.put("/v1/projects/PTH/repo", json={"path": str(repo)})
    with app.state.session_factory() as s:
        with pytest.raises(domain.DomainError) as exc:
            domain.create_commit(s, "PTH", pair[0][:4], "x", [])
        assert exc.value.code == "COMMIT_AMBIGUOUS"
        with pytest.raises(domain.DomainError) as exc:
            domain.create_commit(s, "PTH", "f" * 40, "x", [])
        assert exc.value.code == "COMMIT_NOT_FOUND"
        assert domain.create_commit(s, "PTH", pair[0], "x", []).external_key == pair[0]


def test_requirement_linked_only_by_unverified_commit_is_reported(env, tmp_path):
    """PTH-FR-081: «commit без проверки» — отдельная причина; проверенный коммит её снимает."""
    _, c, _ = env
    c.post("/v1/projects/PTH/artifacts", json={"type": "FR", "title": "Вход"})
    c.post("/v1/projects/PTH/commits", json={"sha": "b" * 40, "title": "без репозитория", "implements": ["PTH-FR-001"]})
    unverified = c.get("/v1/projects/PTH/lost", params={"reason": "unverified_commit"}).json()
    assert [i["display_id"] for i in unverified["items"]] == ["PTH-FR-001"]
    assert unverified["items"][0]["reasons"] == ["unverified_commit"]
    assert c.get("/v1/projects/PTH/lost", params={"reason": "no_code_link"}).json()["total"] == 0
    ctx = c.get("/v1/projects/PTH/context", params={"focus": "PTH-FR-001"}).json()
    assert ctx["lost"]["reasons"] == ["unverified_commit"]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    c.put("/v1/projects/PTH/repo", json={"path": str(repo)})
    head = _git(repo, "rev-parse", "HEAD")
    c.post("/v1/projects/PTH/commits", json={"sha": head, "title": "проверенный", "implements": ["PTH-FR-001"]})
    assert c.get("/v1/projects/PTH/lost", params={"reason": "unverified_commit"}).json()["total"] == 0
