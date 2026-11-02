"""Название TEST уникально в проекте: все пути записи, переименование, гонка, миграция старых дублей."""
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy
from fastapi.testclient import TestClient

from pth.api import create_app
from pth.auth import issue_key
from pth.migrations import MIGRATIONS, migrate

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))
from pth_client import PTHClient  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "u.db"
    app = create_app(f"sqlite:///{db}")
    with app.state.session_factory() as s:
        token = issue_key(s, name="admin", kind="admin")
        s.commit()
    c = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    c.post("/v1/projects", json={"key": "PTH", "name": "PTH"})
    c.post("/v1/projects", json={"key": "OTH", "name": "OTH"})
    return app, c, token, db


def new_test(c, title, key="PTH"):
    return c.post(f"/v1/projects/{key}/artifacts", json={"type": "TEST", "title": title})


def test_duplicate_title_rejected_with_existing_id(env):
    _, c, _, _ = env
    assert new_test(c, "pytest: test_x").status_code == 201
    dup = new_test(c, "  pytest:   test_x ")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "TEST_EXISTS"
    assert "PTH-TEST-001" in dup.json()["error"]["message"]
    assert new_test(c, "pytest: TEST_X").status_code == 201  # регистр различает тесты
    assert new_test(c, "pytest: test_x", key="OTH").status_code == 201  # другой проект
    fr = [c.post("/v1/projects/PTH/artifacts", json={"type": "FR", "title": "одинаковое"}) for _ in range(2)]
    assert [r.status_code for r in fr] == [201, 201]  # правило только для TEST
    assert len(c.get("/v1/projects/PTH/artifacts", params={"type": "TEST"}).json()) == 2


def test_rename_by_revision_checks_and_frees_title(env):
    _, c, _, _ = env
    new_test(c, "a")
    new_test(c, "b")
    taken = c.post("/v1/projects/PTH/artifacts/PTH-TEST-002/revisions", json={"title": "a"})
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "TEST_EXISTS"
    assert c.get("/v1/projects/PTH/artifacts/PTH-TEST-002").json()["title"] == "b"
    assert c.post("/v1/projects/PTH/artifacts/PTH-TEST-001/revisions", json={"title": "c"}).status_code == 201
    assert new_test(c, "a").status_code == 201  # прежнее название освободилось
    same = c.post("/v1/projects/PTH/artifacts/PTH-TEST-002/revisions", json={"title": "b", "text": "правка"})
    assert same.status_code == 201  # новая ревизия со своим же названием — не конфликт


def test_changeset_paths_are_checked_and_roll_back(env):
    _, c, _, _ = env
    new_test(c, "a")
    ops = [{"op": "create_artifact", "display_id": "PTH-FR-010", "type": "FR", "number": 10, "title": "ok"},
           {"op": "create_artifact", "display_id": "PTH-TEST-010", "type": "TEST", "number": 10, "title": "a"}]
    uid = c.post("/v1/projects/PTH/changesets", json={"title": "dup", "operations": ops}).json()["uid"]
    r = c.post(f"/v1/projects/PTH/changesets/{uid}/apply")
    assert r.status_code == 409 and r.json()["error"]["code"] == "TEST_EXISTS"
    assert c.get("/v1/projects/PTH/artifacts/PTH-FR-010").status_code == 404  # откатилось целиком
    assert c.get(f"/v1/projects/PTH/changesets/{uid}").json()["last_error"]
    twin = [{"op": "create_artifact", "display_id": f"PTH-TEST-02{n}", "type": "TEST", "number": 20 + n, "title": "z"}
            for n in range(2)]
    uid = c.post("/v1/projects/PTH/changesets", json={"title": "twin", "operations": twin}).json()["uid"]
    assert c.post(f"/v1/projects/PTH/changesets/{uid}/apply").status_code == 409  # два новых в одном ChangeSet
    new_test(c, "b")
    rename = [{"op": "add_revision", "display_id": "PTH-TEST-002", "title": "a"}]
    uid = c.post("/v1/projects/PTH/changesets", json={"title": "ren", "operations": rename}).json()["uid"]
    assert c.post(f"/v1/projects/PTH/changesets/{uid}/apply").status_code == 409


def test_parallel_creation_leaves_one_test(env):
    app, _, token, _ = env

    def create(_):
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as cl:
            return new_test(cl, "pytest: гонка").status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(create, range(16)))
    assert sorted(set(codes)) == [201, 409] and codes.count(201) == 1  # ни одного 500
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as cl:
        assert len(cl.get("/v1/projects/PTH/artifacts", params={"type": "TEST"}).json()) == 1


def test_index_is_the_backstop_when_code_check_is_bypassed(env, monkeypatch):
    """Если проверка в коде не увидела конкурента (гонка), запись останавливает индекс, ответ — 409, не 500."""
    _, c, _, db = env
    new_test(c, "a")
    import pth.models.artifacts as artifacts
    real = artifacts.select
    monkeypatch.setattr(artifacts, "select", lambda *cols: real(*cols).where(sqlalchemy.false()))
    r = new_test(c, "a")
    assert r.status_code == 409 and r.json()["error"]["code"] == "TEST_EXISTS"
    with sqlite3.connect(db) as con, pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO artifacts (uid, project_key, type, number, display_id, title_key, created_at) "
                    "VALUES ('x', 'PTH', 'TEST', 9, 'PTH-TEST-009', 'a', '2026-01-01')")


def test_migration_keeps_old_duplicates_and_blocks_new_ones(env):
    """База до миграции 10 со старыми дублями: миграция не падает, ключ у меньшего номера, дубль не переименовать в занятое."""
    app, c, _, db = env
    new_test(c, "dup")
    with sqlite3.connect(db) as con:  # откатываем схему к версии 9 и подкладываем старый дубль
        con.execute("DROP INDEX uq_artifacts_test_title")
        con.execute("ALTER TABLE artifacts DROP COLUMN title_key")
        con.execute("DELETE FROM schema_version WHERE version >= 10")
        con.execute("INSERT INTO artifacts (uid, project_key, type, number, display_id, created_at) "
                    "VALUES ('old-dup', 'PTH', 'TEST', 2, 'PTH-TEST-002', '2026-01-01')")
        con.execute("INSERT INTO artifact_revisions (uid, artifact_uid, number, title, text, attributes, created_at) "
                    "VALUES ('old-dup-r1', 'old-dup', 1, ' dup ', '', '{}', '2026-01-01')")
    assert migrate(app.state.session_factory.kw["bind"]) == MIGRATIONS[-1][0]
    with sqlite3.connect(db) as con:
        keys = dict(con.execute("SELECT display_id, title_key FROM artifacts WHERE type = 'TEST'"))
        assert keys == {"PTH-TEST-001": "dup", "PTH-TEST-002": None}
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name = 'uq_artifacts_test_title'").fetchone()
    assert migrate(app.state.session_factory.kw["bind"]) == MIGRATIONS[-1][0]  # повторный запуск ничего не ломает
    back = c.post("/v1/projects/PTH/artifacts/PTH-TEST-002/revisions", json={"title": "dup"})
    assert back.status_code == 409
    assert c.post("/v1/projects/PTH/artifacts/PTH-TEST-002/revisions", json={"title": "ДУБЛЬ → PTH-TEST-001"}).status_code == 201


class _Prefixed:
    def __init__(self, inner):
        self.inner = inner

    def request(self, method, path, **kwargs):
        return self.inner.request(method, "/v1" + path, **kwargs)


def test_client_reuses_test_created_in_parallel(env):
    """Клиент увидел старый список (тест ещё не заведён), сервер ответил TEST_EXISTS — клиент берёт существующий."""
    app, c, token, _ = env
    c.post("/v1/projects/PTH/artifacts", json={"type": "FR", "title": "ФТ"})
    api = PTHClient("http://test", token, http=_Prefixed(TestClient(app)))
    real = api.artifacts
    calls = []

    def stale_then_real(key, type_=None):
        calls.append(type_)
        if len(calls) == 1:
            new_test(c, "pytest:  параллельно")  # кто-то другой успел между чтением и записью
            return []
        return real(key, type_)

    api.artifacts = stale_then_real
    out = api.register_test("PTH", "pytest: параллельно", ["PTH-FR-001"])
    assert out == {"test": "PTH-TEST-001", "created": False, "verifies_added": ["PTH-FR-001"]}
