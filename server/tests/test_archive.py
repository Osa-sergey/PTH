"""Архив вместо удаления (спека §5.4, §7.1; PTH-US-014): ID, ревизии и связи остаются, анализ элемент не видит."""
import pytest
from fastapi.testclient import TestClient

from pth.api import create_app
from pth.auth import issue_key


@pytest.fixture()
def c(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'a.db'}")
    with app.state.session_factory() as s:
        token = issue_key(s, name="admin", kind="admin")
        s.commit()
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    client.post("/v1/projects", json={"key": "PTH", "name": "PTH"})
    return client


P = "/v1/projects/PTH"


def make(c, type_, title, **attrs):
    r = c.post(f"{P}/artifacts", json={"type": type_, "title": title, "attributes": attrs})
    assert r.status_code == 201, r.text
    return r.json()["display_id"]


def link(c, type_, source, target):
    r = c.post(f"{P}/relations", json={"type": type_, "source": source, "target": target})
    assert r.status_code == 201, r.text
    return r.json()


def archive(c, display_id, reason="ошибочно заведён", replaced_by=None):
    return c.post(f"{P}/artifacts/{display_id}/archive",
                  json={"reason": reason, **({"replaced_by": replaced_by} if replaced_by else {})})


def ids(c, path, **params):
    return [x["display_id"] for x in c.get(f"{P}/{path}", params=params).json()]


def test_archived_requirement_leaves_analysis_but_keeps_history(c):
    fr = make(c, "FR", "Импорт")
    keep = make(c, "FR", "Экспорт")
    c.post(f"{P}/artifacts/{fr}/revisions", json={"title": "Импорт спеки", "text": "", "attributes": {}})
    assert c.get(f"{P}/lost", params={"reason": "no_code_link"}).json()["total"] == 2
    out = archive(c, fr).json()
    assert out["archived_at"] and out["archive_reason"] == "ошибочно заведён" and out["revision"] == 2
    assert [i["display_id"] for i in c.get(f"{P}/lost", params={"reason": "no_code_link"}).json()["items"]] == [keep]
    assert [i["display_id"] for i in c.get(f"{P}/compliance").json()["items"]] == [keep]
    assert ids(c, "artifacts") == [keep]
    assert ids(c, "artifacts", archived="only") == [fr]
    assert ids(c, "artifacts", archived="include") == [fr, keep]
    assert ids(c, "search", q="Импорт") == []
    assert len(c.get(f"{P}/artifacts/{fr}/revisions").json()) == 2  # история цела
    assert c.get(f"{P}/artifacts/{fr}").json()["archived_at"]  # карточка открывается
    assert make(c, "FR", "Новое") == "PTH-FR-003"  # номер архивного не выдаётся повторно
    audit = c.get(f"{P}/audit").json()  # новые сверху
    assert [e["action"] for e in audit if e["target"] == fr][0] == "artifact.archive"


def test_archived_artifact_is_read_only_until_restored(c):
    fr = make(c, "FR", "Импорт")
    test = make(c, "TEST", "pytest: t1")
    archive(c, fr)
    archive(c, test)
    revise = c.post(f"{P}/artifacts/{fr}/revisions", json={"title": "x", "text": "", "attributes": {}})
    relation = c.post(f"{P}/relations", json={"type": "verifies", "source": test, "target": fr})
    commit = c.post(f"{P}/commits", json={"sha": "a" * 40, "title": "c", "implements": [fr]})
    evidence = c.post(f"{P}/evidence", json={"test": test, "result": "pass"})
    again = archive(c, fr)
    assert {r.status_code for r in (revise, relation, commit, evidence, again)} == {409}
    assert {r.json()["error"]["code"] for r in (revise, relation, commit, evidence, again)} == {"ARTIFACT_ARCHIVED"}
    restored = c.post(f"{P}/artifacts/{fr}/restore")
    assert restored.status_code == 200 and restored.json()["revision"] == 2 and restored.json()["archived_at"] is None
    assert c.post(f"{P}/artifacts/{fr}/restore").json()["error"]["code"] == "ARTIFACT_NOT_ARCHIVED"
    assert c.post(f"{P}/artifacts/{fr}/revisions", json={"title": "x", "text": "", "attributes": {}}).status_code == 201


def test_archive_checks_reason_replacement_and_type(c):
    fr = make(c, "FR", "Импорт")
    other = make(c, "FR", "Экспорт")
    c.post(f"{P}/commits", json={"sha": "b" * 40, "title": "c", "implements": []})
    assert archive(c, fr, reason="").status_code == 422
    assert archive(c, fr, replaced_by=fr).json()["error"]["code"] == "INVALID_REPLACEMENT"
    assert archive(c, fr, replaced_by="PTH-FR-404").json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
    assert archive(c, "PTH-COMMIT-001").json()["error"]["code"] == "COMMIT_NOT_ARCHIVABLE"
    archive(c, other)
    assert archive(c, fr, replaced_by=other).json()["error"]["code"] == "ARTIFACT_ARCHIVED"
    assert archive(c, fr, replaced_by=None).status_code == 200


def test_duplicate_test_goes_to_archive_and_frees_its_name(c):
    """Дубль TEST — в архив со ссылкой на канонический; название свободно, восстановление снова его проверяет."""
    fr = make(c, "FR", "Импорт")
    canonical = make(c, "TEST", "pytest: dup")
    link(c, "verifies", canonical, fr)
    renamed = make(c, "TEST", "pytest: dup (2)")
    out = archive(c, renamed, reason="дубль", replaced_by=canonical).json()
    assert out["replaced_by"] == canonical
    c.post(f"{P}/evidence", json={"test": canonical, "result": "pass"})
    assert [i["status"] for i in c.get(f"{P}/compliance").json()["items"]] == ["verified"]
    free = make(c, "TEST", "pytest: dup (2)")  # название архивного свободно
    assert c.post(f"{P}/artifacts/{renamed}/restore").json()["error"]["code"] == "TEST_EXISTS"
    archive(c, free)
    assert c.post(f"{P}/artifacts/{renamed}/restore").status_code == 200
    assert c.post(f"{P}/artifacts", json={"type": "TEST", "title": "pytest: dup (2)"}).json()["error"]["code"] == "TEST_EXISTS"


def test_relations_of_archived_artifacts_leave_graph_and_lists(c):
    us = make(c, "US", "Как аналитик, хочу импорт")
    fr = make(c, "FR", "Импорт")
    link(c, "refines", fr, us)
    assert c.get(f"{P}/lost").json()["total"] == 0
    archive(c, fr)
    assert [i["display_id"] for i in c.get(f"{P}/lost").json()["items"]] == [us]  # US снова без ФТ
    assert c.get(f"{P}/relations").json() == []
    assert len(c.get(f"{P}/relations", params={"include_archived": True}).json()) == 1
    g = c.get(f"{P}/graph", params={"focus": fr, "depth": 2}).json()  # граф от архивного — только он сам
    assert [(n["display_id"], n["archived"]) for n in g["nodes"]] == [(fr, True)] and g["edges"] == []
    assert c.get(f"{P}/impact", params={"focus": fr}).status_code == 200
    assert c.get(f"{P}/context", params={"focus": fr}).status_code == 200
    assert [f for f in c.get(f"{P}/review", params={"limit": 500}).json()["findings"] if f["display_id"] == fr] == []


def test_changeset_archives_relations_instead_of_deleting(c):
    us = make(c, "US", "Как аналитик, хочу импорт")
    fr = make(c, "FR", "Импорт")
    first = link(c, "refines", fr, us)
    dup = make(c, "FR", "Импорт (дубль)")
    uid = c.post(f"{P}/changesets", json={"title": "чистка", "operations": [
        {"op": "delete_relation", "type": "refines", "source": fr, "target": us},
        {"op": "archive_artifact", "display_id": dup, "reason": "дубль", "replaced_by": fr}]}).json()["uid"]
    described = c.get(f"{P}/changesets/{uid}/review").json()
    assert [(o["kind"], o["status"]) for o in described["operations"]] == [("unlink", "ok"), ("archive", "ok")]
    assert described["valid"]
    assert c.post(f"{P}/changesets/{uid}/apply").status_code == 200
    assert c.get(f"{P}/relations").json() == []
    assert ids(c, "artifacts", archived="only") == [dup]
    again = link(c, "refines", fr, us)  # связь из архива возвращается, а не дублируется
    assert again["uid"] == first["uid"]
    assert [r["uid"] for r in c.get(f"{P}/relations").json()] == [first["uid"]] and c.get(f"{P}/lost").json()["total"] == 0
    uid = c.post(f"{P}/changesets", json={"title": "вернуть", "operations": [
        {"op": "restore_artifact", "display_id": dup}]}).json()["uid"]
    assert c.post(f"{P}/changesets/{uid}/apply").status_code == 200
    assert ids(c, "artifacts", archived="only") == []


def test_release_shows_archived_item_as_blocker(c):
    us = make(c, "US", "Как аналитик, хочу импорт")
    fr = make(c, "FR", "Импорт")
    link(c, "refines", fr, us)
    rel = c.post(f"{P}/releases", json={"name": "R1"}).json()["uid"]
    c.post(f"{P}/releases/{rel}/items", json={"display_ids": [fr], "with_related": False})
    archive(c, fr)
    progress = c.get(f"{P}/releases/{rel}").json()["progress"]
    assert [(i["display_id"], i["stage"]) for i in progress["items"]] == [(fr, "archived")]
    assert {"display_id": fr, "stage": "archived"} in progress["blockers"]
    add = c.post(f"{P}/releases/{rel}/items", json={"display_ids": [fr]})
    assert add.json()["error"]["code"] == "ARTIFACT_ARCHIVED"
    assert c.delete(f"{P}/releases/{rel}/items/{fr}").status_code == 200


def test_migration_12_adds_archive_columns_to_existing_database(tmp_path):
    import sqlite3
    from pth.db import make_engine
    from pth.migrations import MIGRATIONS, migrate
    path = tmp_path / "old.db"
    engine = make_engine(f"sqlite:///{path}")
    migrate(engine)
    with sqlite3.connect(path) as con:  # база версии 11: без колонок архива
        for table, col in (("artifacts", "archived_at"), ("artifacts", "archive_reason"),
                           ("artifacts", "replaced_by_uid"), ("relations", "archived_at")):
            con.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        con.execute("DELETE FROM schema_version WHERE version >= 12")
    engine.dispose()
    assert migrate(make_engine(f"sqlite:///{path}")) == MIGRATIONS[-1][0] == 12
    with sqlite3.connect(path) as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(artifacts)")}
        assert {"archived_at", "archive_reason", "replaced_by_uid"} <= cols


def test_client_archive_and_register_test_ignore_archived_duplicate(tmp_path):
    """Клиент: archive/restore; register_test не берёт архивный дубль, а находит действующий тест."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))
    from pth_client import PTHClient
    app = create_app(f"sqlite:///{tmp_path / 'k.db'}")
    with app.state.session_factory() as s:
        token = issue_key(s, name="admin", kind="admin")
        s.commit()
    from tests.test_client import _Prefixed
    api = PTHClient("http://t", token=token, http=_Prefixed(TestClient(app)))
    api._request("POST", "/projects", json={"key": "PTH", "name": "PTH"})
    fr = api._request("POST", "/projects/PTH/artifacts", json={"type": "FR", "title": "Импорт"})["display_id"]
    old = api.register_test("PTH", "pytest: t", [fr])["test"]
    assert api.archive("PTH", old, "переименован")["archived_at"]
    fresh = api.register_test("PTH", "pytest: t", [fr])
    assert fresh["created"] and fresh["test"] != old
    with pytest.raises(Exception) as exc:
        api.restore("PTH", old)
    assert getattr(exc.value, "code", None) == "TEST_EXISTS"
