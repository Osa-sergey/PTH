"""Импорт принятой спецификации (спека, §8; SK-01 в минимальном виде).

Импорт строит план операций, который можно: применить сразу (`apply`), посмотреть (`dry_run`)
или сохранить черновиком ChangeSet (`as_changeset`). Планирование только читает данные; запись
выполняет `execute_op` внутри одной транзакции. Таблицы DEC, SK и ADR не импортируются.
"""
import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from pth import domain
from pth.models import Artifact, ArtifactRevision, ImportSession, Relation, new_uid, utcnow

# Любой префикс документа (PTH-FR-001, SHOP-US-12): тип задаёт US/FR/NFR/AC, номер — число в конце ID.
ID_CELL = re.compile(r"^[A-Z][A-Z0-9]{1,9}-(US|FR|NFR|AC)-(\d{1,4})$")
REF = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-US-\d{1,4}\b")
SEPARATOR = re.compile(r"^\|?\s*:?-{3,}")
OUT_OF_SCOPE = re.compile(r"^(DEC|SK|ADR)-\d+$")


@dataclass
class Row:
    id: str
    type: str
    number: int
    title: str
    text: str
    attributes: dict
    basis: str
    line: int = 0
    cols: list[int] = field(default_factory=list)  # ячейки строки, из которых получена формулировка


@dataclass
class Plan:
    operations: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    created: int = 0
    revised: int = 0
    unchanged: int = 0
    relations_created: int = 0
    unparsed: list[str] = field(default_factory=list)
    fragments: list[dict] = field(default_factory=list)


def fragments_for(session, key: str, record) -> list[dict]:
    """Привязка строк источника к требованиям. У импортов без сохранённой привязки (до её появления) она
    восстанавливается: текст источника разбирается заново, строка связывается с элементом по внешнему ID."""
    saved = record.report.get("fragments")
    if saved:
        return saved
    rows, _ = parse_spec(record.source_text)
    out = []
    for row in rows:
        art = session.scalar(select(Artifact).where(Artifact.project_key == key, Artifact.external_key == row.id))
        if art is not None:
            out.append({"display_id": art.display_id, "external_key": row.id, "line": row.line,
                        "end_line": row.line, "via": "table", "cols": row.cols})
    return out


def normalize_fragment(f: dict) -> dict:
    """Приводит привязку к единому виду: у старых записей нет конца диапазона, способа и ячеек."""
    via = f.get("via", "table")
    return {**f, "via": via, "end_line": f.get("end_line", f["line"]), "cols": f.get("cols", [1] if via == "table" else [])}


def locate_quote(text: str, quote: str) -> dict | None:
    """Где в тексте стоит цитата модели: строки и колонки начала и конца. Пробелы и переносы сравниваются нестрого."""
    words = quote.split()
    if not words:
        return None
    found = re.search(r"\s+".join(re.escape(w) for w in words), text)
    if found is None:
        return None
    start, end = found.span()
    return {"line": text.count("\n", 0, start) + 1, "end_line": text.count("\n", 0, end) + 1,
            "start_col": start - (text.rfind("\n", 0, start) + 1), "end_col": end - (text.rfind("\n", 0, end) + 1)}


def proposal_fragments(text: str, items: list[dict]) -> tuple[list[dict], int]:
    """Привязка предложений модели к тексту по цитате. Возвращает привязки и число цитат, которых в тексте нет."""
    out, missing = [], 0
    for item in items:
        where = locate_quote(text, item["quote"])
        if where is None:
            missing += 1
            continue
        out.append({"display_id": item["display_id"], "external_key": None, "via": "llm", "quote": item["quote"],
                    "verified": item["verified"], "cols": [], **where})
    return out, missing


def parse_spec(text: str) -> tuple[list[Row], list[str]]:
    """Разбирает таблицы с колонкой «ID». Возвращает строки и нераспознанные строки с ID."""
    rows: list[Row] = []
    unparsed: list[str] = []
    header: list[str] | None = None
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        if SEPARATOR.match(stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if header[0] != "ID" or not cells:
            continue
        if OUT_OF_SCOPE.match(cells[0]):
            continue
        match = ID_CELL.match(cells[0])
        if not match or len(cells) < 3:
            unparsed.append(stripped[:120])
            continue
        type_ = match.group(1)
        attributes = {}
        if type_ == "US" and len(cells) >= 4 and header[3] == "Приоритет":
            attributes["priority"] = cells[3]
        rows.append(Row(
            id=cells[0], type=type_, number=int(match.group(2)), title=cells[1],
            text=cells[2] if type_ == "US" else "",
            attributes=attributes,
            basis=cells[-1] if type_ != "US" else "",
            line=line_no, cols=[1, 2] if type_ == "US" else [1],
        ))
    return rows, unparsed


def plan_import(session: Session, key: str, text: str) -> Plan:
    """Только чтение: строит операции так, чтобы их применение дало ровно этот отчёт.

    display_id строится от ключа проекта (SHOP-FR-001), внешний ID документа (PTH-FR-001)
    сохраняется как external_key — по спеке §1.5 импортированный проект получает свой ключ.
    """
    domain.get_project(session, key)
    rows, unparsed = parse_spec(text)
    plan = Plan(unparsed=unparsed)
    display = {row.id: f"{key}-{row.type}-{row.number:03d}" for row in rows}
    known: dict[str, Artifact] = {}  # внешний ID -> уже существующий артефакт проекта

    for row in rows:
        artifact = session.scalar(select(Artifact).where(
            Artifact.project_key == key, Artifact.external_key == row.id))
        if artifact is None:
            plan.operations.append({"op": "create_artifact", "display_id": display[row.id],
                                    "external_key": row.id, "type": row.type, "number": row.number,
                                    "title": row.title, "text": row.text, "attributes": row.attributes})
            plan.created += 1
            plan.changes.append({"display_id": display[row.id], "action": "created"})
            continue
        known[row.id] = artifact
        current = domain.latest_revision(session, artifact)
        if (current.title, current.text, current.attributes) == (row.title, row.text, row.attributes):
            plan.unchanged += 1
            continue
        plan.operations.append({"op": "add_revision", "display_id": artifact.display_id, "title": row.title,
                                "text": row.text, "attributes": row.attributes})
        plan.revised += 1
        plan.changes.append({"display_id": artifact.display_id, "action": "revised"})

    for row in rows:
        plan.fragments.append({"display_id": display[row.id], "external_key": row.id, "line": row.line,
                               "end_line": row.line, "via": "table", "cols": row.cols})

    planned: set[tuple[str, str]] = set()
    for row in rows:
        if row.type == "US" or not row.basis:
            continue
        if row.id in known:
            source_display, source_art = known[row.id].display_id, known[row.id]
        else:
            source_display, source_art = display[row.id], None
        for ref in dict.fromkeys(REF.findall(row.basis)):
            if ref in known:
                target_display, target_art = known[ref].display_id, known[ref]
            elif ref in display:
                target_display, target_art = display[ref], None
            else:
                target_art = session.scalar(select(Artifact).where(
                    Artifact.project_key == key, Artifact.external_key == ref))
                if target_art is None:
                    continue  # цели нет ни в файле, ни в проекте
                target_display = target_art.display_id
            pair = (source_display, target_display)
            if pair in planned:
                continue
            if source_art is not None and target_art is not None:
                already = session.scalar(select(Relation).where(
                    Relation.type == "refines", Relation.source_uid == source_art.uid,
                    Relation.target_uid == target_art.uid))
                if already is not None:
                    continue
            planned.add(pair)
            plan.operations.append({"op": "create_relation", "type": "refines",
                                    "source": source_display, "target": target_display})
            plan.relations_created += 1
    return plan


def build_preview(session: Session, key: str, text: str) -> dict:
    """Что даст импорт таблиц, до применения: строки документа как элементы с действием (создать, новая ревизия,
    без изменений), найденные связи и строки, которые не разобрались. Только чтение."""
    domain.get_project(session, key)
    rows, unparsed = parse_spec(text)
    display = {row.id: f"{key}-{row.type}-{row.number:03d}" for row in rows}
    items, known = [], {}
    for row in rows:
        art = session.scalar(select(Artifact).where(Artifact.project_key == key, Artifact.external_key == row.id))
        action, current = "created", None
        if art is not None:
            known[row.id] = art
            rev = domain.latest_revision(session, art)
            current = {"title": rev.title, "text": rev.text, "priority": rev.attributes.get("priority")}
            action = "unchanged" if (rev.title, rev.text, rev.attributes) == (row.title, row.text, row.attributes) else "revised"
        items.append({"ref": row.id, "display_id": art.display_id if art else display[row.id], "external_key": row.id,
                      "type": row.type, "number": row.number, "title": row.title, "text": row.text,
                      "priority": row.attributes.get("priority"), "action": action, "current": current,
                      "line": row.line, "cols": row.cols, "basis": row.basis})
    plan = plan_import(session, key, text)
    in_file = set(known) | set(display)

    def endpoint(display_id: str) -> str:
        """Ссылка на элемент файла по его внешнему ID, иначе — display_id существующего элемента."""
        for ext, d in display.items():
            if d == display_id or (ext in known and known[ext].display_id == display_id):
                return ext
        return display_id
    relations = [{"type": op["type"], "source": endpoint(op["source"]), "target": endpoint(op["target"])}
                 for op in plan.operations if op["op"] == "create_relation"]
    _ = in_file
    return {"items": items, "relations": relations, "unparsed": unparsed}


def execute_op(session: Session, key: str, op: dict) -> None:
    """Применяет одну операцию. Любая ошибка — DomainError; откат делает вызывающий."""
    kind = op.get("op")
    if kind == "create_artifact":
        number = op["number"]
        if session.scalar(select(Artifact).where(Artifact.project_key == key,
                                                 Artifact.display_id == op["display_id"])):
            raise domain.DomainError(409, "ARTIFACT_EXISTS", f"{op['display_id']} уже есть в проекте")
        artifact = Artifact(uid=new_uid(), project_key=key, type=op["type"], number=number,
                            display_id=op["display_id"], external_key=op.get("external_key", op["display_id"]),
                            created_at=utcnow())
        session.add(artifact)
        session.add(ArtifactRevision(uid=new_uid(), artifact_uid=artifact.uid, number=1,
                                     title=op["title"], text=op.get("text", ""),
                                     attributes=op.get("attributes", {}), exemption_reason=None,
                                     created_at=utcnow()))
        session.flush()
    elif kind == "add_revision":
        artifact = domain.require_active(domain.get_artifact(session, key, op["display_id"]))
        current = domain.latest_revision(session, artifact)
        expected = op.get("expected_revision")
        if expected is not None and expected != current.number:
            raise domain.DomainError(409, "REVISION_CONFLICT",
                                     f"{op['display_id']}: ожидалась ревизия {expected}, текущая {current.number}")
        session.add(ArtifactRevision(uid=new_uid(), artifact_uid=artifact.uid, number=current.number + 1,
                                     title=op["title"], text=op.get("text", ""),
                                     attributes=op.get("attributes", {}),
                                     exemption_reason=op.get("exemption_reason", current.exemption_reason),
                                     created_at=utcnow()))
        session.flush()
    elif kind == "create_relation":
        domain.create_relation(session, key, op["type"], op["source"], op["target"])
    elif kind == "delete_relation":  # имя операции прежнее; связь уходит в архив, а не стирается (спека §7.1)
        domain.archive_relation(session, key, op["type"], op["source"], op["target"])
    elif kind == "archive_artifact":
        domain.archive_artifact(session, key, op["display_id"], op.get("reason", ""), op.get("replaced_by"))
    elif kind == "restore_artifact":
        domain.restore_artifact(session, key, op["display_id"])
    else:
        raise domain.DomainError(422, "INVALID_OPERATION", f"Неизвестная операция: {kind!r}")


def _title_of(session: Session, key: str, display_id: str, fresh: dict[str, str]) -> str | None:
    if display_id in fresh:
        return fresh[display_id]
    art = session.scalar(select(Artifact).where(Artifact.project_key == key, Artifact.display_id == display_id))
    return domain.latest_revision(session, art).title if art else None


def describe_operations(session: Session, key: str, operations: list[dict], check: bool) -> list[dict]:
    """Операции черновика человеческим языком: что создаётся, чем отличается новая ревизия, какая связь.
    check=True дополнительно проверяет, что операции применились бы: выполняет их по очереди и откатывает всё.
    Первая ошибка останавливает проверку, остальные операции получают статус skipped."""
    out: list[dict] = []
    fresh: dict[str, str] = {}  # заголовки элементов, создаваемых этим же черновиком
    stopped = False
    for index, op in enumerate(operations):
        kind = op.get("op")
        item: dict = {"index": index, "kind": kind}
        if kind == "create_artifact":
            item.update(kind="create", display_id=op["display_id"], type=op["type"], title=op["title"], text=op.get("text", ""),
                        priority=(op.get("attributes") or {}).get("priority"), external_key=op.get("external_key"),
                        provenance=(op.get("attributes") or {}).get("provenance"))
            fresh[op["display_id"]] = op["title"]
        elif kind == "add_revision":
            art = session.scalar(select(Artifact).where(Artifact.project_key == key, Artifact.display_id == op["display_id"]))
            before = None
            if art is not None:
                rev = domain.latest_revision(session, art)
                if not check and op.get("expected_revision") is not None:
                    # Применённое изменение сравнивается с ревизией, от которой оно было сделано, а не с нынешней.
                    rev = session.scalar(select(ArtifactRevision).where(ArtifactRevision.artifact_uid == art.uid,
                                                                       ArtifactRevision.number == op["expected_revision"])) or rev
                before = {"title": rev.title, "text": rev.text, "priority": rev.attributes.get("priority"), "revision": rev.number}
            attrs = op.get("attributes") or {}
            item.update(kind="revise", display_id=op["display_id"], title=op["title"], text=op.get("text", ""), priority=attrs.get("priority"),
                        before=before, expected_revision=op.get("expected_revision"))
            fresh.setdefault(op["display_id"], op["title"])
        elif kind in ("archive_artifact", "restore_artifact"):
            item.update(kind="archive" if kind == "archive_artifact" else "restore", display_id=op["display_id"],
                        title=_title_of(session, key, op["display_id"], fresh), reason=op.get("reason"),
                        replaced_by=op.get("replaced_by"))
        elif kind in ("create_relation", "delete_relation"):
            item.update(kind="link" if kind == "create_relation" else "unlink", type=op["type"], source=op["source"], target=op["target"],
                        source_title=_title_of(session, key, op["source"], fresh), target_title=_title_of(session, key, op["target"], fresh))
        if check:
            if stopped:
                item["status"] = "skipped"
            else:
                try:
                    execute_op(session, key, op)
                    item["status"] = "ok"
                except domain.DomainError as exc:
                    item.update(status="error", error=exc.message, code=exc.code)
                    stopped = True
        out.append(item)
    if check:
        session.rollback()  # проверка ничего не пишет
    return out


def apply_operations(session: Session, key: str, operations: list[dict]) -> None:
    for op in operations:
        execute_op(session, key, op)


def apply_import(session: Session, key: str, source_name: str, text: str,
                 dry_run: bool = False, as_changeset: bool = False) -> dict:
    """Возвращает отчёт. dry_run — откат; as_changeset — черновик без записи данных."""
    plan = plan_import(session, key, text)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    counts = {"created": plan.created, "revised": plan.revised, "unchanged": plan.unchanged,
              "relations_created": plan.relations_created, "unparsed": plan.unparsed,
              "changes": plan.changes, "fragments": plan.fragments}
    report = {"uid": None, "changeset_uid": None, "source_name": source_name, "source_sha256": sha,
              "created_at": utcnow(), "dry_run": dry_run, **counts}

    if as_changeset:
        from pth.models import ChangeSet
        cs = ChangeSet(uid=new_uid(), project_key=key, title=f"Импорт {source_name}", state="draft",
                       operations=plan.operations, created_at=utcnow(),
                       source={"name": source_name, "sha256": sha, "text": text, "report": counts})
        session.add(cs)
        session.flush()
        report["changeset_uid"] = cs.uid
        return report

    apply_operations(session, key, plan.operations)
    if dry_run:
        session.rollback()
        return report
    record = ImportSession(uid=new_uid(), project_key=key, source_name=source_name, source_sha256=sha,
                           source_text=text, report=counts, created_at=report["created_at"])
    session.add(record)
    session.flush()
    report["uid"] = record.uid
    return report


def record_from_changeset(session: Session, key: str, source: dict) -> str:
    """Создаёт запись ImportSession при применении черновика из импорта."""
    record = ImportSession(uid=new_uid(), project_key=key, source_name=source["name"],
                           source_sha256=source["sha256"], source_text=source["text"],
                           report=source["report"], created_at=utcnow())
    session.add(record)
    session.flush()
    return record.uid


def report_of(record: ImportSession) -> dict:
    return {"uid": record.uid, "changeset_uid": None, "source_name": record.source_name, "dry_run": False,
            "source_sha256": record.source_sha256, "created_at": record.created_at, **record.report}
