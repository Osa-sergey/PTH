"""Клиент API Project Traceability Hub для скриптов, CI и Claude-скиллов.

    from pth_client import PTHClient
    api = PTHClient("http://127.0.0.1:8000", token=os.environ["PTH_TOKEN"])
    report = api.import_spec("PTH", "spec.md", text, mode="dry_run")

Командная строка (переменные PTH_URL и PTH_TOKEN; `sdk/pth` сам выбирает Python ≥ 3.10 с httpx, обычно server/.venv):
    sdk/pth projects
    sdk/pth import PTH путь/к/spec.md --dry-run
    sdk/pth import PTH путь/к/spec.md --changeset
    sdk/pth apply-changeset PTH <uid>
    sdk/pth artifact PTH PTH-US-001
    sdk/pth context PTH PTH-US-001 --depth 2
    sdk/pth search PTH "импорт" --type FR
    sdk/pth status PTH
    sdk/pth me
    sdk/pth lost PTH --type FR
    sdk/pth graph PTH PTH-US-001 --depth 2
    sdk/pth test PTH "pytest: test_x" --verifies PTH-FR-001 --coverage partial --gap "нет отказа сети"
    sdk/pth archive PTH PTH-TEST-007 --reason "дубль" --replaced-by PTH-TEST-003
    sdk/pth restore PTH PTH-TEST-007

Зависимость: httpx. Ошибки API поднимаются как PTHError с полями status, code, message.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx


class PTHError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class PTHClient:
    PAGE = 500  # максимум limit у списков API

    def __init__(self, base_url: str, token: str, http: Any = None, timeout: float = 60.0):
        self._token = token
        self._http = http if http is not None else httpx.Client(base_url=base_url.rstrip("/") + "/v1", timeout=timeout)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", **kwargs.pop("headers", {})}
        response = self._http.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                err = response.json()["error"]
                raise PTHError(response.status_code, err["code"], err["message"])
            except (ValueError, KeyError):
                raise PTHError(response.status_code, f"HTTP_{response.status_code}", response.text[:200])
        return response.json()

    # --- себя и контекст ------------------------------------------------------
    def me(self) -> dict:
        return self._request("GET", "/me")

    def context(self, key: str, focus: str, depth: int = 1) -> dict:
        return self._request("GET", f"/projects/{key}/context", params={"focus": focus, "depth": depth})

    def search(self, key: str, q: str, type_: str | None = None) -> list[dict]:
        params = {"q": q, **({"type": type_} if type_ else {})}
        return self._request("GET", f"/projects/{key}/search", params=params)

    def review(self, key: str, severity: str | None = None) -> dict:
        return self._request("GET", f"/projects/{key}/review", params={"severity": severity} if severity else None)

    def propose_changes(self, key: str, title: str, operations: list[dict]) -> dict:
        """Черновик ChangeSet. Ничего не меняет в проекте до apply_changeset."""
        return self._request("POST", f"/projects/{key}/changesets", json={"title": title, "operations": operations})

    def attach(self, key: str, evidence_uid: str, path: str, name: str | None = None) -> dict:
        """Приложить файл (отчёт нагрузки, SARIF, Allure-архив) к уже записанному запуску."""
        from pathlib import Path as _P
        file = _P(path)
        return self._request("POST", f"/projects/{key}/evidence/{evidence_uid}/attachments",
                             params={"name": name or file.name}, content=file.read_bytes(),
                             headers={"Content-Type": "application/octet-stream"})

    def sync_commits(self, key: str) -> dict:
        """Зарегистрировать в PTH все коммиты подключённого репозитория, которых ещё нет (идемпотентно)."""
        return self._request("POST", f"/projects/{key}/git/sync")

    def link_commit(self, key: str, sha: str, implements: list[str]) -> dict:
        """Добавить связи implements к уже зарегистрированному коммиту."""
        return self._request("POST", f"/projects/{key}/commits/{sha}/implements", json={"implements": implements})

    def tests_status(self, key: str) -> dict:
        return self._request("GET", f"/projects/{key}/tests")

    def impact(self, key: str, focus: str, depth: int = 2) -> dict:
        return self._request("GET", f"/projects/{key}/impact", params={"focus": focus, "depth": depth})

    def impact_rules(self, key: str) -> dict:
        return self._request("GET", f"/projects/{key}/impact-rules")

    def register_commit(self, key: str, sha: str, title: str, implements: list[str]) -> dict:
        """Регистрирует коммит и связывает его с требованиями (implements). Это claim, не доказательство."""
        return self._request("POST", f"/projects/{key}/commits",
                             json={"sha": sha, "title": title, "implements": implements})

    def record_evidence(self, key: str, test: str, result: str, commit_sha: str | None = None,
                        environment: dict | None = None) -> dict:
        body = {"test": test, "result": result, "commit_sha": commit_sha, "environment": environment or {}}
        return self._request("POST", f"/projects/{key}/evidence", json=body)

    def compliance(self, key: str) -> dict:
        return self._request("GET", f"/projects/{key}/compliance")

    def readiness(self, key: str) -> dict:
        return self._request("GET", f"/projects/{key}/readiness")

    def psi(self, key: str) -> str:
        response = self._http.get(f"/projects/{key}/psi", headers={"Authorization": f"Bearer {self._token}"})
        if response.status_code >= 400:
            raise PTHError(response.status_code, f"HTTP_{response.status_code}", response.text[:200])
        return response.text

    def create_baseline(self, key: str, name: str) -> dict:
        return self._request("POST", f"/projects/{key}/baselines", json={"name": name})

    def baselines(self, key: str) -> list[dict]:
        return self._request("GET", f"/projects/{key}/baselines")

    def status(self, key: str) -> dict:
        return self._request("GET", f"/projects/{key}/status")

    # --- проекты и артефакты --------------------------------------------------
    def projects(self) -> list[dict]:
        return self._request("GET", "/projects")

    def create_project(self, key: str, name: str) -> dict:
        return self._request("POST", "/projects", json={"key": key, "name": name})

    def artifacts(self, key: str, type_: str | None = None) -> list[dict]:
        """Все артефакты проекта. API отдаёт страницами (по умолчанию 20), поэтому читаем до конца:
        неполный список приводил к дублям TEST при проверке «уже заведён?»."""
        items: list[dict] = []
        while True:
            params = {"offset": len(items), "limit": self.PAGE, **({"type": type_} if type_ else {})}
            page = self._request("GET", f"/projects/{key}/artifacts", params=params)
            items += page
            if len(page) < self.PAGE:
                return items

    def relations(self, key: str) -> list[dict]:
        return self._request("GET", f"/projects/{key}/relations")

    def register_test(self, key: str, title: str, verifies: list[str], coverage: str = "full",
                      gaps: list[str] | None = None, _retry: bool = True) -> dict:
        """Заводит TEST со связями verifies. Повторный вызов с тем же названием не создаёт дубль, а
        добавляет недостающие связи: артефакты неизменяемы и через API не удаляются. Требования
        проверяются до создания, чтобы опечатка в ID не оставила TEST без связей."""
        targets = [self.artifact(key, d)["display_id"] for d in verifies]
        norm = " ".join(title.split())  # как ключ уникальности на сервере: пробелы схлопнуты, регистр важен
        same = [a for a in self.artifacts(key, "TEST") if " ".join(a["title"].split()) == norm]
        if same:  # при старых дублях канонический — с меньшим номером (сортировка по строке ломается после 999)
            test, created = min(same, key=lambda a: int(a["display_id"].rsplit("-", 1)[1])), False
        else:
            attributes = {"coverage": coverage, **({"coverage_gaps": gaps} if gaps else {})}
            try:
                test = self._request("POST", f"/projects/{key}/artifacts",
                                     json={"type": "TEST", "title": title, "attributes": attributes})
                created = True
            except PTHError as exc:
                if exc.code != "TEST_EXISTS" or not _retry:
                    raise
                # тот же тест параллельно завёл кто-то ещё — берём его
                return self.register_test(key, title, verifies, coverage, gaps, _retry=False)
        tid = test["display_id"]
        linked = {r["target"] for r in self.relations(key) if r["type"] == "verifies" and r["source"] == tid}
        added = []
        for t in targets:
            if t in linked:
                continue
            try:
                self._request("POST", f"/projects/{key}/relations", json={"type": "verifies", "source": tid, "target": t})
                added.append(t)
            except PTHError as exc:
                if exc.code != "DUPLICATE_RELATION":  # связь параллельно добавил кто-то ещё — она уже есть
                    raise
        result = {"test": tid, "created": created, "verifies_added": added}
        if not created and test["attributes"].get("coverage", "full") != coverage:
            result["note"] = (f"У существующего {tid} coverage={test['attributes'].get('coverage', 'full')}; "
                              "это поле здесь не меняется — нужна новая ревизия артефакта")
        return result

    def artifact(self, key: str, display_id: str) -> dict:
        return self._request("GET", f"/projects/{key}/artifacts/{display_id}")

    def archive(self, key: str, display_id: str, reason: str, replaced_by: str | None = None) -> dict:
        """В архив вместо удаления: ревизии, связи и ID остаются, из анализа элемент выпадает."""
        body = {"reason": reason, **({"replaced_by": replaced_by} if replaced_by else {})}
        return self._request("POST", f"/projects/{key}/artifacts/{display_id}/archive", json=body)

    def restore(self, key: str, display_id: str) -> dict:
        return self._request("POST", f"/projects/{key}/artifacts/{display_id}/restore")

    def lost(self, key: str, type_: str | None = None, priority: str | None = None,
             reason: str | None = None) -> dict:
        params = {k: v for k, v in {"type": type_, "priority": priority, "reason": reason}.items() if v}
        return self._request("GET", f"/projects/{key}/lost", params=params)

    def graph(self, key: str, focus: str, depth: int = 2, direction: str = "both",
              relation_types: str | None = None) -> dict:
        params = {"focus": focus, "depth": depth, "direction": direction}
        if relation_types:
            params["relation_types"] = relation_types
        return self._request("GET", f"/projects/{key}/graph", params=params)

    # --- импорт и черновики ----------------------------------------------------
    def import_spec(self, key: str, source_name: str, text: str, mode: str = "dry_run") -> dict:
        """mode: dry_run (без записи), changeset (черновик), apply (сразу записать)."""
        if mode not in {"dry_run", "changeset", "apply"}:
            raise ValueError("mode: dry_run | changeset | apply")
        params = {"dry_run": mode == "dry_run", "as_changeset": mode == "changeset"}
        return self._request("POST", f"/projects/{key}/imports", params=params,
                             json={"source_name": source_name, "text": text})

    def changesets(self, key: str) -> list[dict]:
        return self._request("GET", f"/projects/{key}/changesets")

    def apply_changeset(self, key: str, uid: str) -> dict:
        return self._request("POST", f"/projects/{key}/changesets/{uid}/apply")

    # --- выделение моделью -----------------------------------------------------
    def llm_models(self) -> list[str]:
        return self._request("GET", "/llm/models")

    def extract(self, key: str, model: str, text: str) -> dict:
        return self._request("POST", f"/projects/{key}/extractions", json={"model": model, "text": text})

    def start_extraction_job(self, key: str, model: str, source_name: str, text: str) -> dict:
        return self._request("POST", f"/projects/{key}/extractions/jobs",
                             json={"model": model, "source_name": source_name, "text": text})

    def extraction_job(self, key: str, uid: str) -> dict:
        return self._request("GET", f"/projects/{key}/extractions/jobs/{uid}")


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pth_client", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("projects")
    sub.add_parser("me")
    p_ev = sub.add_parser("evidence", help="записать результат проверки")
    p_ev.add_argument("key")
    p_ev.add_argument("test")
    p_ev.add_argument("result", choices=["pass", "fail", "blocked", "inconclusive"])
    p_ev.add_argument("--commit")
    sub.add_parser("compliance").add_argument("key")
    sub.add_parser("readiness").add_argument("key")
    sub.add_parser("psi").add_argument("key")
    p_bl = sub.add_parser("baseline", help="зафиксировать базовый снимок")
    p_bl.add_argument("key")
    p_bl.add_argument("name")
    sub.add_parser("baselines").add_argument("key")
    p_imp2 = sub.add_parser("impact")
    p_imp2.add_argument("key")
    p_imp2.add_argument("focus")
    p_imp2.add_argument("--depth", type=int, default=2)
    p_attach = sub.add_parser("attach", help="приложить файл отчёта к запуску теста")
    p_attach.add_argument("key")
    p_attach.add_argument("evidence")
    p_attach.add_argument("path")
    p_imp_rules = sub.add_parser("impact-rules", help="встроенные и свои правила влияния с пояснениями")
    p_imp_rules.add_argument("key")
    p_test = sub.add_parser("test", help="завести TEST со связями verifies (повтор с тем же названием не дублирует)")
    p_test.add_argument("key")
    p_test.add_argument("title", help="название, например «pytest: test_import_dry_run»")
    p_test.add_argument("--verifies", nargs="+", required=True)
    p_test.add_argument("--coverage", choices=["full", "partial"], default="full")
    p_test.add_argument("--gap", action="append", help="что не покрыто (обязательно при partial; можно несколько)")
    p_arch = sub.add_parser("archive", help="убрать артефакт в архив (вместо удаления; ID и история остаются)")
    p_arch.add_argument("key")
    p_arch.add_argument("display_id")
    p_arch.add_argument("--reason", required=True)
    p_arch.add_argument("--replaced-by", help="display_id элемента, который его заменяет")
    p_rest = sub.add_parser("restore", help="вернуть артефакт из архива новой ревизией")
    p_rest.add_argument("key")
    p_rest.add_argument("display_id")
    p_com = sub.add_parser("commit")
    p_com.add_argument("key")
    p_com.add_argument("sha")
    p_com.add_argument("title")
    p_com.add_argument("--implements", nargs="+", required=True)
    p_rev = sub.add_parser("review")
    p_rev.add_argument("key")
    p_rev.add_argument("--severity", choices=["error", "warning", "info"])
    p_prop = sub.add_parser("propose", help="черновик из JSON-файла: {\"title\": ..., \"operations\": [...]}")
    p_prop.add_argument("key")
    p_prop.add_argument("file")
    p_status = sub.add_parser("status")
    p_status.add_argument("key")
    p_ctx = sub.add_parser("context")
    p_ctx.add_argument("key")
    p_ctx.add_argument("focus")
    p_ctx.add_argument("--depth", type=int, default=1)
    p_search = sub.add_parser("search")
    p_search.add_argument("key")
    p_search.add_argument("q")
    p_search.add_argument("--type")
    p_imp = sub.add_parser("import", help="обязателен --dry-run или --changeset; прямой apply из CLI не делается")
    p_imp.add_argument("key")
    p_imp.add_argument("file")
    mode = p_imp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--changeset", action="store_true")
    sub.add_parser("changesets").add_argument("key")
    p_apply = sub.add_parser("apply-changeset")
    p_apply.add_argument("key")
    p_apply.add_argument("uid")
    p_art = sub.add_parser("artifact")
    p_art.add_argument("key")
    p_art.add_argument("display_id")
    p_list = sub.add_parser("artifacts")
    p_list.add_argument("key")
    p_list.add_argument("--type")
    p_lost = sub.add_parser("lost")
    p_lost.add_argument("key")
    p_lost.add_argument("--type")
    p_lost.add_argument("--priority")
    p_lost.add_argument("--reason", choices=["no_reflection", "no_code_link", "unverified_commit"],
                        help="no_reflection — потерянные (по умолчанию); no_code_link — не реализованные; "
                             "unverified_commit — реализованы только коммитами с непроверенным SHA")
    p_graph = sub.add_parser("graph")
    p_graph.add_argument("key")
    p_graph.add_argument("focus")
    p_graph.add_argument("--depth", type=int, default=2)
    args = parser.parse_args(argv)

    base = os.environ.get("PTH_URL", "http://127.0.0.1:8000")
    token = os.environ.get("PTH_TOKEN")
    if not token:
        print("Задайте PTH_TOKEN (ключ выпускается через scripts/create_key.py)", file=sys.stderr)
        return 2
    api = PTHClient(base, token)
    try:
        if args.cmd == "projects":
            result = api.projects()
        elif args.cmd == "me":
            result = api.me()
        elif args.cmd == "evidence":
            result = api.record_evidence(args.key, args.test, args.result, args.commit)
        elif args.cmd == "compliance":
            result = api.compliance(args.key)
        elif args.cmd == "readiness":
            result = api.readiness(args.key)
        elif args.cmd == "psi":
            print(api.psi(args.key))
            return 0
        elif args.cmd == "baseline":
            result = api.create_baseline(args.key, args.name)
        elif args.cmd == "baselines":
            result = api.baselines(args.key)
        elif args.cmd == "impact":
            result = api.impact(args.key, args.focus, depth=args.depth)
        elif args.cmd == "attach":
            result = api.attach(args.key, args.evidence, args.path)
        elif args.cmd == "impact-rules":
            result = api.impact_rules(args.key)
        elif args.cmd == "test":
            if args.coverage == "partial" and not args.gap:
                p_test.error("при --coverage partial укажите --gap: что не покрыто")
            result = api.register_test(args.key, args.title, args.verifies, args.coverage, args.gap)
        elif args.cmd == "archive":
            result = api.archive(args.key, args.display_id, args.reason, args.replaced_by)
        elif args.cmd == "restore":
            result = api.restore(args.key, args.display_id)
        elif args.cmd == "commit":
            result = api.register_commit(args.key, args.sha, args.title, args.implements)
        elif args.cmd == "review":
            result = api.review(args.key, args.severity)
        elif args.cmd == "propose":
            body = json.loads(Path(args.file).read_text(encoding="utf-8"))
            result = api.propose_changes(args.key, body["title"], body["operations"])
        elif args.cmd == "status":
            result = api.status(args.key)
        elif args.cmd == "context":
            result = api.context(args.key, args.focus, depth=args.depth)
        elif args.cmd == "search":
            result = api.search(args.key, args.q, args.type)
        elif args.cmd == "import":
            path = Path(args.file)
            mode_name = "changeset" if args.changeset else "dry_run"
            result = api.import_spec(args.key, path.name, path.read_text(encoding="utf-8"), mode=mode_name)
        elif args.cmd == "changesets":
            result = api.changesets(args.key)
        elif args.cmd == "apply-changeset":
            result = api.apply_changeset(args.key, args.uid)
        elif args.cmd == "artifact":
            result = api.artifact(args.key, args.display_id)
        elif args.cmd == "artifacts":
            result = api.artifacts(args.key, args.type)
        elif args.cmd == "lost":
            result = api.lost(args.key, args.type, args.priority, args.reason)
        else:
            result = api.graph(args.key, args.focus, depth=args.depth)
    except PTHError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
