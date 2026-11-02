"""Git и CI: подключение репозитория, проверка коммита, прогоны от CI, правило соответствия."""
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pth.api import create_app
from pth.auth import issue_key


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    (path / "check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    _git(path, "add", "check.py")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add check")
    return path


@pytest.fixture()
def env(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'g.db'}")
    with app.state.session_factory() as s:
        admin = issue_key(s, name="admin", kind="admin")
        ci = issue_key(s, name="ci", kind="service", project_key="PTH", role="writer")
        s.commit()
    return app, admin, ci


def client_for(app, token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def seed(c):
    c.post("/v1/projects", json={"key": "PTH", "name": "PTH"})
    c.post("/v1/projects/PTH/artifacts", json={"type": "FR", "title": "Вход"})
    c.post("/v1/projects/PTH/artifacts", json={"type": "TEST", "title": "тест входа"})
    c.post("/v1/projects/PTH/relations", json={"type": "verifies", "source": "PTH-TEST-001", "target": "PTH-FR-001"})


def test_connect_repo_is_admin_only_and_validates_git(env, repo):
    app, admin, ci = env
    with client_for(app, admin) as a:
        a.post("/v1/projects", json={"key": "PTH", "name": "PTH"})
        ok = a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
        assert ok.status_code == 200 and ok.json()["head"]
        bad = a.put("/v1/projects/PTH/repo", json={"path": str(repo.parent)})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "REPO_INVALID"
        assert a.get("/v1/projects/PTH/repo").json()["available"] is True
    with client_for(app, ci) as c:
        assert c.put("/v1/projects/PTH/repo", json={"path": str(repo)}).status_code == 403


def test_ci_run_on_real_commit_is_verified_and_counts_for_compliance(env, repo):
    app, admin, ci = env
    head = _git(repo, "rev-parse", "HEAD")
    with client_for(app, admin) as a:
        seed(a)
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
    with client_for(app, ci) as c:
        short = head[:10]
        r = c.post("/v1/projects/PTH/ci/runs", json={"commit": short, "results": [{"test": "PTH-TEST-001", "result": "pass"}]})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["commit_oid"] == head and body["verified"] is True and body["trust_level"] == "trusted_ci"
    with client_for(app, admin) as a:
        ev = a.get("/v1/projects/PTH/evidence").json()[0]
        assert ev["verified"] is True and ev["issuer"] == "ci" and ev["commit_oid"] == head
        comp = a.get("/v1/projects/PTH/compliance").json()
        assert comp["items"][0]["status"] == "verified"


def test_ci_run_on_unknown_commit_is_rejected(env, repo):
    app, admin, ci = env
    with client_for(app, admin) as a:
        seed(a)
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
    with client_for(app, ci) as c:
        r = c.post("/v1/projects/PTH/ci/runs", json={"commit": "deadbeef12", "results": [{"test": "PTH-TEST-001", "result": "pass"}]})
        assert r.status_code == 422 and r.json()["error"]["code"] == "COMMIT_NOT_FOUND"


def test_ci_run_without_repo_stays_pending_and_is_not_counted(env):
    app, admin, ci = env
    with client_for(app, admin) as a:
        seed(a)
    with client_for(app, ci) as c:
        r = c.post("/v1/projects/PTH/ci/runs", json={"commit": "abcdef1234", "results": [{"test": "PTH-TEST-001", "result": "pass"}]})
        assert r.status_code == 201 and r.json()["verified"] is False
    with client_for(app, admin) as a:
        assert a.get("/v1/projects/PTH/compliance").json()["items"][0]["status"] == "not_verified"
        assert a.get("/v1/projects/PTH/repo").json()["connected"] is False


def test_ci_result_must_target_a_test(env, repo):
    app, admin, ci = env
    with client_for(app, admin) as a:
        seed(a)
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
    with client_for(app, ci) as c:
        r = c.post("/v1/projects/PTH/ci/runs", json={"commit": _git(repo, "rev-parse", "HEAD"),
                                                     "results": [{"test": "PTH-FR-001", "result": "pass"}]})
        assert r.status_code == 422 and r.json()["error"]["code"] == "NOT_A_TEST"


def test_evidence_for_old_commit_is_stale_and_test_status_shows_it(env, repo):
    app, admin, ci = env
    head = _git(repo, "rev-parse", "HEAD")
    with client_for(app, admin) as a:
        seed(a)
        a.post("/v1/projects/PTH/artifacts", json={"type": "TEST", "title": "без запуска", "attributes": {"command": "pytest"}})
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
    with client_for(app, ci) as c:
        c.post("/v1/projects/PTH/ci/runs", json={"commit": head, "results": [{"test": "PTH-TEST-001", "result": "pass"}]})
    with client_for(app, admin) as a:
        assert a.get("/v1/projects/PTH/compliance").json()["items"][0]["status"] == "verified"
        st = a.get("/v1/projects/PTH/tests").json()
        assert st["head"] == head and st["counts"] == {"passed_current": 1, "not_run": 1}
        assert st["requirements_without_test"] == []
        # новый коммит: старое свидетельство больше не действует для HEAD
        (repo / "more.txt").write_text("x", encoding="utf-8")
        _git(repo, "add", "more.txt")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "next")
        assert a.get("/v1/projects/PTH/compliance").json()["items"][0]["status"] == "stale"
        st2 = a.get("/v1/projects/PTH/tests").json()
        assert st2["tests"][0]["state"] == "passed_old_commit"


def test_partial_test_set_is_recorded_not_counted_as_full(env, repo):
    """Все тесты прошли, но набор неполный: статус partial, готовность не ready."""
    app, admin, ci = env
    head = _git(repo, "rev-parse", "HEAD")
    with client_for(app, admin) as a:
        seed(a)
        r = a.post("/v1/projects/PTH/artifacts", json={"type": "TEST", "title": "неполный",
                   "attributes": {"coverage": "partial"}})
        assert r.status_code == 422 and r.json()["error"]["code"] == "COVERAGE_GAPS_REQUIRED"
        ok = a.post("/v1/projects/PTH/artifacts", json={"type": "TEST", "title": "вход (частично)",
                    "attributes": {"coverage": "partial", "coverage_gaps": "нет проверок ошибок пароля"}})
        assert ok.status_code == 201
        a.post("/v1/projects/PTH/relations", json={"type": "verifies", "source": ok.json()["display_id"], "target": "PTH-FR-001"})
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
    with client_for(app, ci) as c:
        c.post("/v1/projects/PTH/ci/runs", json={"commit": head, "results": [{"test": "PTH-TEST-001", "result": "pass"},
                                                                          {"test": "PTH-TEST-002", "result": "pass"}]})
    with client_for(app, admin) as a:
        st = a.get("/v1/projects/PTH/tests").json()
        partial = [t for t in st["tests"] if t["coverage"] == "partial"]
        assert partial and partial[0]["coverage_gaps"] == "нет проверок ошибок пароля"
        comp = a.get("/v1/projects/PTH/compliance").json()
        item = next(i for i in comp["items"] if i["display_id"] == "PTH-FR-001")
        assert item["status"] == "partial"
        assert a.get("/v1/projects/PTH/readiness").json()["verdict"] != "ready"


def test_git_commits_list_and_detail_without_registration(env, repo):
    """Коммиты читаются из Git сами: регистрация не нужна, проверки и упоминания требований подтягиваются."""
    app, admin, ci = env
    (repo / "extra.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _git(repo, "add", "extra.py")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=Автор", "commit", "-q", "-m", "feat: вход PTH-FR-001\n\nПодробности реализации.")
    head = _git(repo, "rev-parse", "HEAD")
    with client_for(app, admin) as a:
        assert a.get("/v1/projects/PTH/git/commits").status_code in (404, 409)  # проекта/репозитория нет
        seed(a)
        assert a.get("/v1/projects/PTH/git/commits").json()["error"]["code"] == "REPO_NOT_CONNECTED"
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
        a.post("/v1/projects/PTH/ci/runs", json={"commit": head, "results": [{"test": "PTH-TEST-001", "result": "pass"}]})
        r = a.get("/v1/projects/PTH/git/commits")
        assert r.headers["x-total-count"] == "3"
        top = r.json()[0]
        assert top["sha"] == head and top["registered"] == "PTH-COMMIT-001" and top["mentions"] == ["PTH-FR-001"]  # CI сам зарегистрировал коммит
        assert top["checks"] == {"total": 1, "passed": 1, "failed": 0}
        d = a.get(f"/v1/projects/PTH/git/commits/{head[:8]}").json()
        assert d["author"] == "Автор" and d["is_head"] is True and d["body"] == "Подробности реализации."
        assert d["files"] == [{"path": "extra.py", "added": 2, "deleted": 0, "status": "A"}]
        assert d["mentions"][0]["id"] == "PTH-FR-001" and d["runs"][0]["test"] == "PTH-TEST-001"
        assert a.get("/v1/projects/PTH/git/commits/deadbeef").status_code == 404


def test_git_diff_and_repo_info(env, repo):
    app, admin, ci = env
    (repo / "check.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n", encoding="utf-8")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=Автор", "commit", "-qam", "правка check")
    head = _git(repo, "rev-parse", "HEAD")
    with client_for(app, admin) as a:
        seed(a)
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
        d = a.get(f"/v1/projects/PTH/git/commits/{head[:8]}/diff", params={"path": "check.py"}).json()
        lines = d["hunks"][0]["lines"]
        assert [x["type"] for x in lines] == ["ctx", "add", "ctx"]
        assert lines[1] == {"type": "add", "old": None, "new": 2, "text": "print('ok')"} and lines[0]["old"] == 1
        assert a.get(f"/v1/projects/PTH/git/commits/{head[:8]}/diff", params={"path": "../x"}).status_code == 404
        info = a.get("/v1/projects/PTH/git/info").json()
        assert info["commits"] == 3 and info["head"]["sha"] == head and info["files"] == 1
        assert info["authors"][0]["commits"] >= 1 and info["extensions"][0]["ext"] == ".py"


def test_commits_register_automatically_by_ci_and_sync(env, repo):
    app, admin, ci = env
    head = _git(repo, "rev-parse", "HEAD")
    with client_for(app, admin) as a:
        seed(a)
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
        assert a.get("/v1/projects/PTH/commits").json() == []
        a.post("/v1/projects/PTH/ci/runs", json={"commit": head, "results": [{"test": "PTH-TEST-001", "result": "pass"}]})
        a.post("/v1/projects/PTH/ci/runs", json={"commit": head, "results": [{"test": "PTH-TEST-001", "result": "pass"}]})
        assert [c["sha"] for c in a.get("/v1/projects/PTH/commits").json()] == [head]  # повтор не дублирует
        s = a.post("/v1/projects/PTH/git/sync").json()
        assert s == {"registered": 1, "in_git": 2, "already": 1}
        assert a.post("/v1/projects/PTH/git/sync").json()["registered"] == 0
        # ручная регистрация уже занятого SHA по-прежнему 409, а связи добавляются отдельным вызовом
        assert a.post("/v1/projects/PTH/commits", json={"sha": head, "title": "x", "implements": ["PTH-FR-001"]}).status_code == 409
        r = a.post(f"/v1/projects/PTH/commits/{head}/implements", json={"implements": ["PTH-FR-001"]})
        assert r.status_code == 200 and r.json()["added"] == ["PTH-FR-001"]
        assert a.post(f"/v1/projects/PTH/commits/{head}/implements", json={"implements": ["PTH-FR-001"]}).json()["added"] == []
        assert a.post("/v1/projects/PTH/commits/ffffffff/implements", json={"implements": ["PTH-FR-001"]}).status_code == 404


def test_release_lifecycle_scope_freeze_progress_and_tagged_release(env, repo):
    app, admin, ci = env
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v1")
    with client_for(app, admin) as a:
        seed(a)  # FR-001, TEST-001 -> FR-001
        a.post("/v1/projects/PTH/artifacts", json={"type": "US", "title": "Как пользователь, хочу войти", "attributes": {"priority": "P0"}})
        a.post("/v1/projects/PTH/artifacts", json={"type": "AC", "title": "Вход проверен"})
        a.post("/v1/projects/PTH/relations", json={"type": "refines", "source": "PTH-FR-001", "target": "PTH-US-001"})
        a.post("/v1/projects/PTH/relations", json={"type": "refines", "source": "PTH-AC-001", "target": "PTH-FR-001"})
        a.post("/v1/projects/PTH/relations", json={"type": "verifies", "source": "PTH-TEST-001", "target": "PTH-AC-001"})
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
        rel = a.post("/v1/projects/PTH/releases", json={"name": "0.1"}).json()
        uid = rel["uid"]
        assert a.post("/v1/projects/PTH/releases", json={"name": "0.1"}).status_code == 409
        assert a.post(f"/v1/projects/PTH/releases/{uid}/items", json={"display_ids": ["PTH-TEST-001"]}).status_code == 422
        added = a.post(f"/v1/projects/PTH/releases/{uid}/items", json={"display_ids": ["PTH-US-001"]}).json()["added"]
        assert [x["display_id"] for x in added] == ["PTH-US-001", "PTH-AC-001", "PTH-FR-001"] or len(added) == 3
        assert a.post(f"/v1/projects/PTH/releases/{uid}/items", json={"display_ids": ["PTH-US-001"]}).json()["added"] == []
        prog = a.get(f"/v1/projects/PTH/releases/{uid}").json()["progress"]
        assert prog["total"] == 3 and prog["counts"] == {"not_started": 3} and prog["percent"] == 0

        assert a.post(f"/v1/projects/PTH/releases/{uid}/freeze").json()["status"] == "frozen"
        assert a.post(f"/v1/projects/PTH/releases/{uid}/items", json={"display_ids": ["PTH-FR-001"]}).status_code == 409
        # выпуск без тега/с неизвестным тегом
        assert a.post(f"/v1/projects/PTH/releases/{uid}/release", json={"tag": "nope"}).json()["error"]["code"] == "TAG_NOT_FOUND"
        # есть незакрытое — нужна причина
        blocked = a.post(f"/v1/projects/PTH/releases/{uid}/release", json={"tag": "v1"})
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "BLOCKERS_PRESENT"

        # реализация и проверка: коммит с implements и прогон теста на нём
        a.post("/v1/projects/PTH/commits", json={"sha": head, "title": "реализация", "implements": ["PTH-FR-001", "PTH-AC-001"]})
        a.post("/v1/projects/PTH/ci/runs", json={"commit": head, "results": [{"test": "PTH-TEST-001", "result": "pass"}]})
        a.post("/v1/projects/PTH/artifacts/PTH-FR-001/revisions", json={"title": "Вход v2", "text": ""})  # правка после фиксации
        prog = a.get(f"/v1/projects/PTH/releases/{uid}").json()["progress"]
        assert prog["changed_after_freeze"] == ["PTH-FR-001"]
        by = {i["display_id"]: i for i in prog["items"]}
        assert by["PTH-AC-001"]["stage"] == "verified" and by["PTH-FR-001"]["stage"] == "implemented"  # у ФТ нет своего теста

        # выпуск с причиной: блокер остаётся в снимке готовности
        done = a.post(f"/v1/projects/PTH/releases/{uid}/release", json={"tag": "v1", "reason": "ФТ без теста принимаем осознанно"}).json()
        assert done["status"] == "released" and done["commit"] == head and done["tag"] == "v1"
        assert done["readiness"]["blockers"] and done["decision_reason"].startswith("ФТ без теста")
        assert a.post(f"/v1/projects/PTH/releases/{uid}/cancel", json={"reason": "x"}).status_code == 409

        r2 = a.post("/v1/projects/PTH/releases", json={"name": "0.2"}).json()["uid"]
        a.post(f"/v1/projects/PTH/releases/{r2}/items", json={"display_ids": ["PTH-AC-001"], "with_related": False})
        a.post(f"/v1/projects/PTH/releases/{r2}/freeze")
        assert a.post(f"/v1/projects/PTH/releases/{r2}/release", json={"tag": "v1"}).json()["error"]["code"] == "TAG_USED"

        sm = a.get("/v1/projects/PTH/story-map").json()
        card = sm["cards"][0]
        assert sm["activities"] == ["пользователь"] and card["display_id"] == "PTH-US-001" and card["release"] == uid
        assert card["requirements"][0]["display_id"] == "PTH-FR-001"
        assert [r["name"] for r in sm["releases"]] == ["0.1", "0.2"]


def test_release_move_compare_and_report(env, repo):
    app, admin, ci = env
    with client_for(app, admin) as a:
        seed(a)
        a.post("/v1/projects/PTH/artifacts", json={"type": "US", "title": "Как пользователь, хочу войти"})
        a.post("/v1/projects/PTH/relations", json={"type": "refines", "source": "PTH-FR-001", "target": "PTH-US-001"})
        r1 = a.post("/v1/projects/PTH/releases", json={"name": "0.1"}).json()["uid"]
        r2 = a.post("/v1/projects/PTH/releases", json={"name": "0.2"}).json()["uid"]
        a.post(f"/v1/projects/PTH/releases/{r1}/items", json={"display_ids": ["PTH-US-001"]})
        m = a.post("/v1/projects/PTH/releases/move", json={"display_id": "PTH-US-001", "from_release": r1, "to_release": r2}).json()
        assert sorted(m["removed"]) == ["PTH-FR-001", "PTH-US-001"] and sorted(m["added"]) == ["PTH-FR-001", "PTH-US-001"]
        assert a.get(f"/v1/projects/PTH/releases/{r1}").json()["progress"]["total"] == 0
        # в бэклог и обратно
        assert a.post("/v1/projects/PTH/releases/move", json={"display_id": "PTH-US-001", "from_release": r2, "to_release": None}).status_code == 200
        a.post("/v1/projects/PTH/releases/move", json={"display_id": "PTH-US-001", "from_release": None, "to_release": r2})
        a.post(f"/v1/projects/PTH/releases/{r1}/items", json={"display_ids": ["PTH-US-001"], "with_related": False})
        a.post(f"/v1/projects/PTH/releases/{r2}/freeze")
        assert a.post("/v1/projects/PTH/releases/move", json={"display_id": "PTH-US-001", "from_release": r1, "to_release": r2}).json()["error"]["code"] == "RELEASE_LOCKED"
        cmp = a.get("/v1/projects/PTH/releases/compare", params={"a": r1, "b": r2}).json()
        assert [x["display_id"] for x in cmp["added"]] == ["PTH-FR-001"] and cmp["removed"] == [] and cmp["same"] == 1
        md = a.get(f"/v1/projects/PTH/releases/{r2}/report")
        assert md.headers["content-type"].startswith("text/markdown")
        assert "# Релиз 0.2" in md.text and "| PTH-FR-001 |" in md.text and "## Не закрыто" in md.text and "## Проверки требований" in md.text


def test_usm_matches_release_composition(env):
    """USM и вкладка релизов видят одно и то же: US релиза стоит в строке этого релиза, иначе — в «не запланировано»."""
    app, admin, ci = env
    with client_for(app, admin) as a:
        a.post("/v1/projects", json={"key": "PTH", "name": "PTH"})
        for i in range(1, 5):
            a.post("/v1/projects/PTH/artifacts", json={"type": "US", "title": f"Как пользователь {i}, хочу делать {i}"})
        a.post("/v1/projects/PTH/artifacts", json={"type": "FR", "title": "ФТ первой"})
        a.post("/v1/projects/PTH/relations", json={"type": "refines", "source": "PTH-FR-001", "target": "PTH-US-001"})
        r1 = a.post("/v1/projects/PTH/releases", json={"name": "0.1"}).json()["uid"]
        r2 = a.post("/v1/projects/PTH/releases", json={"name": "0.2"}).json()["uid"]
        a.post(f"/v1/projects/PTH/releases/{r1}/items", json={"display_ids": ["PTH-US-001", "PTH-US-002"]})
        a.post(f"/v1/projects/PTH/releases/{r2}/items", json={"display_ids": ["PTH-US-003"]})
        a.post("/v1/projects/PTH/releases/move", json={"display_id": "PTH-US-002", "from_release": r1, "to_release": r2})

        sm = a.get("/v1/projects/PTH/story-map").json()
        assert len(sm["cards"]) == 4  # все US на карте
        for uid in (r1, r2):
            in_release = {i["display_id"] for i in a.get(f"/v1/projects/PTH/releases/{uid}").json()["progress"]["items"] if i["type"] == "US"}
            on_map = {c["display_id"] for c in sm["cards"] if c["release"] == uid}
            assert in_release == on_map, (uid, in_release, on_map)
        assert {c["display_id"] for c in sm["cards"] if c["release"] is None} == {"PTH-US-004"}
        # ФТ под карточкой совпадает с составом релиза, взятым вместе с US
        card = next(c for c in sm["cards"] if c["display_id"] == "PTH-US-001")
        assert [r["display_id"] for r in card["requirements"]] == ["PTH-FR-001"]
        assert "PTH-FR-001" in {i["display_id"] for i in a.get(f"/v1/projects/PTH/releases/{r1}").json()["progress"]["items"]}
        # отменённый релиз с карты уходит, его US возвращаются в «не запланировано»
        a.post(f"/v1/projects/PTH/releases/{r2}/cancel", json={"reason": "не нужен"})
        sm = a.get("/v1/projects/PTH/story-map").json()
        assert [r["name"] for r in sm["releases"]] == ["0.1"]
        assert {c["display_id"] for c in sm["cards"] if c["release"] is None} == {"PTH-US-002", "PTH-US-003", "PTH-US-004"}


def test_release_marks_requirements_without_story_and_usm_marks_dependencies(env):
    app, admin, ci = env
    with client_for(app, admin) as a:
        a.post("/v1/projects", json={"key": "PTH", "name": "PTH"})
        a.post("/v1/projects/PTH/artifacts", json={"type": "US", "title": "Как пользователь, хочу A"})
        a.post("/v1/projects/PTH/artifacts", json={"type": "FR", "title": "ФТ1"})
        a.post("/v1/projects/PTH/artifacts", json={"type": "FR", "title": "ФТ2"})
        a.post("/v1/projects/PTH/artifacts", json={"type": "FR", "title": "ФТ без US"})
        for fr in ("PTH-FR-001", "PTH-FR-002"):
            a.post("/v1/projects/PTH/relations", json={"type": "refines", "source": fr, "target": "PTH-US-001"})
        r = a.post("/v1/projects/PTH/releases", json={"name": "0.1"}).json()["uid"]
        # US одна, без зависимостей; ФТ без US отдельно; ФТ2 тоже без US в релизе
        a.post(f"/v1/projects/PTH/releases/{r}/items", json={"display_ids": ["PTH-US-001"], "with_related": False})
        a.post(f"/v1/projects/PTH/releases/{r}/items", json={"display_ids": ["PTH-FR-003"], "with_related": False})
        card = a.get("/v1/projects/PTH/story-map").json()["cards"][0]
        assert card["dependencies"] == {"taken": 0, "total": 2}
        a.post(f"/v1/projects/PTH/releases/{r}/items", json={"display_ids": ["PTH-FR-001"], "with_related": False})
        assert a.get("/v1/projects/PTH/story-map").json()["cards"][0]["dependencies"] == {"taken": 1, "total": 2}
        a.post(f"/v1/projects/PTH/releases/{r}/items", json={"display_ids": ["PTH-US-001"], "with_related": True})
        assert a.get("/v1/projects/PTH/story-map").json()["cards"][0]["dependencies"] == {"taken": 2, "total": 2}
        prog = a.get(f"/v1/projects/PTH/releases/{r}").json()["progress"]
        by = {i["display_id"]: i for i in prog["items"]}
        assert by["PTH-FR-003"]["without_story"] is True and by["PTH-FR-003"]["stories"] == []
        assert by["PTH-FR-001"]["without_story"] is False and by["PTH-US-001"]["without_story"] is False
        assert prog["without_story"] == ["PTH-FR-003"]
        # если убрать US, её требования становятся «без US» и называют отсутствующую
        a.post("/v1/projects/PTH/releases/move", json={"display_id": "PTH-US-001", "from_release": r, "to_release": None, "with_related": False})
        by = {i["display_id"]: i for i in a.get(f"/v1/projects/PTH/releases/{r}").json()["progress"]["items"]}
        assert by["PTH-FR-001"]["without_story"] is True and by["PTH-FR-001"]["stories_missing"] == ["PTH-US-001"]


def test_register_commit_checks_sha_against_connected_repo(env, repo):
    """PTH-FR-046: при подключённом репозитории неизвестный SHA не регистрируется, короткий разворачивается."""
    app, admin, _ = env
    head = _git(repo, "rev-parse", "HEAD")
    with client_for(app, admin) as a:
        seed(a)
        unverified = a.post("/v1/projects/PTH/commits", json={"sha": "0" * 40, "title": "до репозитория", "implements": []})
        assert unverified.status_code == 201 and unverified.json()["attributes"]["sha_verified"] is False
        a.put("/v1/projects/PTH/repo", json={"path": str(repo)})
        fake = a.post("/v1/projects/PTH/commits", json={"sha": "1" * 40, "title": "нет такого", "implements": ["PTH-FR-001"]})
        assert fake.status_code == 422 and fake.json()["error"]["code"] == "COMMIT_NOT_FOUND"
        short = a.post("/v1/projects/PTH/commits", json={"sha": head[:7], "title": "реализация", "implements": ["PTH-FR-001"]})
        assert short.status_code == 201
        assert short.json()["attributes"] == {"sha": head, "sha_verified": True}
        dup = a.post("/v1/projects/PTH/commits", json={"sha": head, "title": "повтор", "implements": []})
        assert dup.status_code == 409  # короткий и полный SHA — один коммит
        a.post("/v1/projects/PTH/git/sync")
        synced = [x for x in a.get("/v1/projects/PTH/artifacts", params={"type": "COMMIT"}).json() if x["external_key"] != head
                  and x["external_key"] != "0" * 40]
        assert synced and all(x["attributes"]["sha_verified"] is True for x in synced)
        repo.rename(repo.parent / "moved")
        gone = a.post("/v1/projects/PTH/commits", json={"sha": "3" * 40, "title": "репозиторий пропал", "implements": []})
        assert gone.status_code == 422 and gone.json()["error"]["code"] == "REPO_UNAVAILABLE"
