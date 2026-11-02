"""Клиент sdk/pth_client.py против живого приложения (TestClient как транспорт)."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))
from pth_client import PTHClient, PTHError  # noqa: E402

from pth.api import create_app  # noqa: E402
from pth.auth import issue_key  # noqa: E402

SNIPPET = "| ID | Требование | Основание |\n| --- | --- | --- |\n| PTH-FR-001 | Проекты | PTH-US-001 |\n"


@pytest.fixture()
def api(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'c.db'}")
    with app.state.session_factory() as s:
        token = issue_key(s, name="sdk", kind="admin")
        s.commit()
    http = TestClient(app)
    client = PTHClient("http://test", token, http=_Prefixed(http))
    client.create_project("PTH", "PTH")
    return client


class _Prefixed:
    """Добавляет /v1 к относительным путям клиента: TestClient работает с полными URL."""
    def __init__(self, inner):
        self.inner = inner

    def request(self, method, path, **kwargs):
        return self.inner.request(method, "/v1" + path, **kwargs)


def test_import_dry_run_then_changeset_then_apply(api):
    report = api.import_spec("PTH", "spec.md", SNIPPET, mode="dry_run")
    assert report["dry_run"] and report["created"] == 1 and api.artifacts("PTH") == []
    draft = api.import_spec("PTH", "spec.md", SNIPPET, mode="changeset")
    assert draft["changeset_uid"] and api.artifacts("PTH") == []
    api.apply_changeset("PTH", draft["changeset_uid"])
    assert api.artifact("PTH", "PTH-FR-001")["title"] == "Проекты"


def test_errors_are_raised_with_codes(api):
    with pytest.raises(PTHError) as exc:
        api.artifact("PTH", "PTH-FR-999")
    assert exc.value.status == 404 and exc.value.code == "ARTIFACT_NOT_FOUND"


def test_lost_and_graph_helpers(api):
    api.import_spec("PTH", "spec.md", SNIPPET, mode="apply")
    assert api.lost("PTH", type_="FR", reason="no_code_link")["total"] == 1
    graph = api.graph("PTH", "PTH-FR-001", depth=1)
    assert graph["focus"] == "PTH-FR-001" and len(graph["nodes"]) >= 1


def test_invalid_mode_is_rejected_locally(api):
    with pytest.raises(ValueError):
        api.import_spec("PTH", "s.md", SNIPPET, mode="commit")


def test_artifacts_reads_all_pages(api):
    """Раньше клиент получал только первую страницу (20): проверка «тест уже заведён?» его не видела."""
    for n in range(25):
        api._request("POST", "/projects/PTH/artifacts", json={"type": "FR", "title": f"ФТ {n}"})
    assert len(api.artifacts("PTH")) == 25
    api.PAGE = 10  # несколько страниц подряд, включая неполную последнюю
    assert [a["title"] for a in api.artifacts("PTH", "FR")] == [f"ФТ {n}" for n in range(25)]


def test_register_test_is_idempotent_and_links_verifies(api):
    api.import_spec("PTH", "spec.md", SNIPPET + "| PTH-FR-002 | Импорт | PTH-US-001 |\n", mode="apply")
    first = api.register_test("PTH", "pytest: test_x", ["PTH-FR-001"], coverage="partial", gaps=["нет отказа сети"])
    assert first == {"test": "PTH-TEST-001", "created": True, "verifies_added": ["PTH-FR-001"]}
    again = api.register_test("PTH", "pytest: test_x", ["PTH-FR-001", "PTH-FR-002"], coverage="partial")
    assert again == {"test": "PTH-TEST-001", "created": False, "verifies_added": ["PTH-FR-002"]}
    assert len(api.artifacts("PTH", "TEST")) == 1
    full = api.register_test("PTH", "pytest: test_x", ["PTH-FR-001"])
    assert full["verifies_added"] == [] and "coverage=partial" in full["note"]


def test_register_test_checks_targets_before_creating(api):
    with pytest.raises(PTHError) as exc:
        api.register_test("PTH", "pytest: test_typo", ["PTH-FR-404"])
    assert exc.value.code == "ARTIFACT_NOT_FOUND" and api.artifacts("PTH", "TEST") == []


def test_register_test_partial_requires_gaps(api):
    api.import_spec("PTH", "spec.md", SNIPPET, mode="apply")
    with pytest.raises(PTHError) as exc:
        api.register_test("PTH", "pytest: test_y", ["PTH-FR-001"], coverage="partial")
    assert exc.value.code == "COVERAGE_GAPS_REQUIRED" and api.artifacts("PTH", "TEST") == []


def test_register_test_reuses_lowest_numbered_duplicate(api, tmp_path):
    """Дубли TEST, заведённые раньше (скриптами в обход клиента): берётся канонический — с меньшим номером."""
    import sqlite3
    api.import_spec("PTH", "spec.md", SNIPPET, mode="apply")
    api._request("POST", "/projects/PTH/artifacts", json={"type": "TEST", "title": "pytest: dup"})
    with sqlite3.connect(tmp_path / "c.db") as con:  # старый дубль
        con.execute("INSERT INTO artifacts (uid, project_key, type, number, display_id, created_at) "
                    "VALUES ('d', 'PTH', 'TEST', 2, 'PTH-TEST-002', '2026-01-01')")
        con.execute("INSERT INTO artifact_revisions (uid, artifact_uid, number, title, text, attributes, created_at) "
                    "VALUES ('d1', 'd', 1, 'pytest: dup', '', '{}', '2026-01-01')")
    assert len(api.artifacts("PTH", "TEST")) == 2
    assert api.register_test("PTH", "pytest: dup", ["PTH-FR-001"])["test"] == "PTH-TEST-001"
