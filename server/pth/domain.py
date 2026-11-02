"""Доменные правила: создание, ревизии, связи, список потерянных, граф.

Реализован подмножество каталога связей спеки (§6.1): refines, motivates, implements.
Все операции выполняются в одной сессии; вызывающий делает commit.
"""
import re
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pth.models import Artifact, ArtifactRevision, Project, Relation, new_uid, utcnow

REQUIREMENT_TYPES = {"FR", "NFR", "AC"}
VERIFIABLE_TYPES = {"FR", "NFR", "AC"}
RELATION_RULES: dict[str, tuple[set[str], set[str]]] = {
    "refines": ({"FR", "NFR", "AC", "US"}, {"US", "FR", "NFR"}),
    "motivates": ({"US", "FR", "NFR"}, {"ADR"}),
    "implements": ({"COMMIT"}, {"FR", "NFR", "AC", "US"}),
    "verifies": ({"TEST"}, {"FR", "NFR", "AC"}),
    # Схема процесса: описание процесса — US или ФТ
    "illustrates": ({"DIAGRAM"}, {"US", "FR"}),
}
RECOMMENDED = {
    "no_code_link": "связать с коммитом (implements) или указать обоснованное исключение",
    "no_reflection": "добавить ФТ с refines к этой US или ADR с motivates",
    "unverified_commit": "подключить репозиторий проекта и зарегистрировать коммит заново: SHA не проверен по Git",
}


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Analysis:
    by_uid: dict[str, Artifact] = field(default_factory=dict)
    revisions: dict[str, ArtifactRevision] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    implemented_by: dict[str, Artifact] = field(default_factory=dict)
    reflected: set[str] = field(default_factory=set)
    # реализованы только коммитами с sha_verified=false (PTH-FR-081: «commit без проверки»); старые коммиты без флага не считаются
    unverified_only: set[str] = field(default_factory=set)

    def reasons(self, artifact: Artifact) -> list[str]:
        rev = self.revisions[artifact.uid]
        if rev.exemption_reason or artifact.type == "COMMIT" or artifact.type == "ADR":
            return []
        found = []
        if artifact.type in REQUIREMENT_TYPES and artifact.uid not in self.implemented_by:
            found.append("no_code_link")
        elif artifact.uid in self.unverified_only:
            found.append("unverified_commit")
        if artifact.type == "US" and artifact.uid not in self.reflected:
            found.append("no_reflection")
        return found


# --- поиск -------------------------------------------------------------------

def get_project(session: Session, key: str) -> Project:
    project = session.get(Project, key)
    if project is None:
        raise DomainError(404, "PROJECT_NOT_FOUND", f"Проект {key} не найден")
    return project


def get_artifact(session: Session, key: str, display_id: str) -> Artifact:
    artifact = session.scalar(
        select(Artifact).where(Artifact.project_key == key, Artifact.display_id == display_id)
    )
    if artifact is None:
        raise DomainError(404, "ARTIFACT_NOT_FOUND", f"Артефакт {display_id} не найден")
    return artifact


def latest_revision(session: Session, artifact: Artifact) -> ArtifactRevision:
    return session.scalar(
        select(ArtifactRevision)
        .where(ArtifactRevision.artifact_uid == artifact.uid)
        .order_by(ArtifactRevision.number.desc())
        .limit(1)
    )


def _next_number(session: Session, key: str, type_: str) -> int:
    current = session.scalar(
        select(Artifact.number)
        .where(Artifact.project_key == key, Artifact.type == type_)
        .order_by(Artifact.number.desc())
        .limit(1)
    )
    return (current or 0) + 1


# --- команды -------------------------------------------------------------------

def create_project(session: Session, key: str, name: str) -> Project:
    if session.get(Project, key) is not None:
        raise DomainError(409, "PROJECT_EXISTS", f"Проект {key} уже существует")
    project = Project(key=key, name=name, created_at=utcnow())
    session.add(project)
    session.flush()
    return project


def _add_artifact(session, key, type_, title, text, attributes, exemption_reason, external_key=None):
    number = _next_number(session, key, type_)
    artifact = Artifact(
        uid=new_uid(), project_key=key, type=type_, number=number,
        display_id=f"{key}-{type_}-{number:03d}", external_key=external_key, created_at=utcnow(),
    )
    session.add(artifact)
    session.add(ArtifactRevision(
        uid=new_uid(), artifact_uid=artifact.uid, number=1, title=title, text=text,
        attributes=attributes, exemption_reason=exemption_reason, created_at=utcnow(),
    ))
    session.flush()
    return artifact


def check_test_coverage(type_: str, attributes: dict) -> None:
    """Неполный набор тестов должен перечислять, что именно не покрыто, иначе он не фиксируется как неполный."""
    if type_ != "TEST":
        return
    coverage = attributes.get("coverage", "full")
    if coverage not in {"full", "partial"}:
        raise DomainError(422, "INVALID_COVERAGE", "coverage: full | partial")
    if coverage == "partial" and not str(attributes.get("coverage_gaps", "")).strip():
        raise DomainError(422, "COVERAGE_GAPS_REQUIRED",
                          "Неполный набор тестов: укажите coverage_gaps — что не покрыто")


MAX_DIAGRAM = 1024 * 1024
FORBIDDEN_SVG = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


def check_diagram(text: str, attributes: dict) -> dict:
    """Схема хранится текстом: Mermaid или SVG. Формат — атрибут format. SVG проверяется как XML без DOCTYPE и сущностей;
    выполнение скриптов исключено на стороне показа (SVG выводится как изображение), здесь они отклоняются заранее."""
    fmt = attributes.get("format")
    if fmt not in {"mermaid", "svg"}:
        raise DomainError(422, "DIAGRAM_FORMAT", "У схемы нужен атрибут format: mermaid или svg")
    if not text.strip():
        raise DomainError(422, "DIAGRAM_EMPTY", "Текст схемы пустой")
    if len(text.encode("utf-8")) > MAX_DIAGRAM:
        raise DomainError(422, "DIAGRAM_TOO_LARGE", "Схема больше 1 МБ")
    if fmt == "svg":
        import xml.etree.ElementTree as ET
        if FORBIDDEN_SVG.search(text):
            raise DomainError(422, "DIAGRAM_SVG_UNSAFE", "В SVG недопустимы DOCTYPE и объявления сущностей")
        if re.search(r"<script|\son\w+\s*=|javascript:", text, re.IGNORECASE):
            raise DomainError(422, "DIAGRAM_SVG_UNSAFE", "В SVG недопустимы скрипты и обработчики событий")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise DomainError(422, "DIAGRAM_SVG_INVALID", f"SVG не разбирается как XML: {exc}")
        if not root.tag.endswith("}svg") and root.tag != "svg":
            raise DomainError(422, "DIAGRAM_SVG_INVALID", "Корневой элемент должен быть svg")
    return attributes


def create_artifact(session, key, type_, title, text, attributes, exemption_reason):
    get_project(session, key)
    if type_ == "COMMIT":
        raise DomainError(422, "USE_COMMIT_ENDPOINT", "Коммиты создаются через /commits")
    check_test_coverage(type_, attributes)
    if type_ == "DIAGRAM":
        attributes = check_diagram(text, attributes)
    return _add_artifact(session, key, type_, title, text, attributes, exemption_reason)


def add_revision(session, key, display_id, title, text, attributes, exemption_reason, expected_revision):
    artifact = get_artifact(session, key, display_id)
    if artifact.type == "TEST":
        check_test_coverage("TEST", attributes)
    if artifact.type == "DIAGRAM":
        attributes = check_diagram(text, attributes)
    current = latest_revision(session, artifact)
    if expected_revision is not None and expected_revision != current.number:
        raise DomainError(
            409, "REVISION_CONFLICT",
            f"Ожидалась ревизия {expected_revision}, текущая {current.number}",
        )
    rev = ArtifactRevision(
        uid=new_uid(), artifact_uid=artifact.uid, number=current.number + 1, title=title,
        text=text, attributes=attributes, exemption_reason=exemption_reason, created_at=utcnow(),
    )
    session.add(rev)
    session.flush()
    return artifact, rev


def create_relation(session, key, type_, source_id, target_id) -> Relation:
    source = get_artifact(session, key, source_id)
    target = get_artifact(session, key, target_id)
    if source.uid == target.uid:
        raise DomainError(422, "INVALID_RELATION", "Связь с самим собой запрещена")
    allowed_source, allowed_target = RELATION_RULES[type_]
    if source.type not in allowed_source or target.type not in allowed_target:
        raise DomainError(
            422, "INVALID_RELATION",
            f"{type_}: {source.type} → {target.type} не разрешено каталогом связей",
        )
    exists = session.scalar(select(Relation).where(
        Relation.type == type_, Relation.source_uid == source.uid, Relation.target_uid == target.uid,
    ))
    if exists is not None:
        raise DomainError(409, "DUPLICATE_RELATION", "Такая связь уже есть")
    relation = Relation(uid=new_uid(), project_key=key, type=type_, source_uid=source.uid,
                        target_uid=target.uid, status="confirmed", created_at=utcnow())
    session.add(relation)
    session.flush()
    return relation


def create_commit(session, key, sha, title, implements):
    """SHA проверяется по подключённому репозиторию (PTH-FR-046): неизвестный коммит не регистрируется,
    короткий SHA разворачивается в полный. Без репозитория коммит принимается с sha_verified=false."""
    from pth import gitops
    from pth.models import ProjectRepo
    get_project(session, key)
    repo = session.get(ProjectRepo, key)
    verified = False
    if repo is not None:
        try:
            oid = gitops.resolve_commit(repo.path, sha)
            if oid is None:
                gitops.validate_repo(repo.path)  # пропавший каталог — не «коммита нет», а репозиторий недоступен
        except gitops.GitError as exc:
            raise DomainError(422, "REPO_UNAVAILABLE", f"Подключённый репозиторий недоступен: {exc}")
        if oid is None and len(sha) < 40 and gitops.prefix_matches(repo.path, sha) > 1:
            raise DomainError(422, "COMMIT_AMBIGUOUS", f"Короткий SHA {sha} неоднозначен в репозитории: укажите длиннее")
        if oid is None:
            raise DomainError(422, "COMMIT_NOT_FOUND", f"Коммит {sha} не найден в подключённом репозитории")
        sha, verified = oid, True
    if session.scalar(select(Artifact).where(
        Artifact.project_key == key, Artifact.external_key == sha,
    )) is not None:
        raise DomainError(409, "COMMIT_EXISTS", f"Коммит {sha} уже зарегистрирован")
    targets = [get_artifact(session, key, d) for d in implements]
    for t in targets:
        if t.type not in RELATION_RULES["implements"][1]:
            raise DomainError(422, "INVALID_RELATION", f"Коммит не может реализовывать {t.type}")
    commit = _add_artifact(session, key, "COMMIT", title, sha, {"sha": sha, "sha_verified": verified}, None,
                           external_key=sha)
    for t in targets:
        session.add(Relation(uid=new_uid(), project_key=key, type="implements",
                             source_uid=commit.uid, target_uid=t.uid, status="confirmed",
                             created_at=utcnow()))
    session.flush()
    return commit


def ensure_commit(session, key, sha: str, title: str, author: str = "", committed_at: str = ""):
    """Регистрирует коммит как COMMIT без связей, если его ещё нет. Возвращает (элемент, создан ли)."""
    existing = session.scalar(select(Artifact).where(Artifact.project_key == key, Artifact.external_key == sha))
    if existing is not None:
        return existing, False
    attrs = {"sha": sha, "sha_verified": True,  # коммит прочитан из подключённого репозитория
             **({"author": author} if author else {}), **({"committed_at": committed_at} if committed_at else {})}
    commit = _add_artifact(session, key, "COMMIT", (title or sha[:10])[:300], sha, attrs, None, external_key=sha)
    return commit, True


def sync_commits(session, key: str, repo_path: str, limit: int = 1000) -> dict:
    """Регистрирует все коммиты текущей ветки, которых ещё нет в PTH. Старые первыми, чтобы номера шли по хронологии."""
    from pth import gitops
    get_project(session, key)
    items = gitops.log_page(repo_path, 0, limit)["items"]
    created = 0
    for row in reversed(items):
        _, new = ensure_commit(session, key, row["sha"], row["title"], row["author"], row["date"])
        created += new
    session.flush()
    return {"registered": created, "in_git": len(items), "already": len(items) - created}


def link_commit(session, key: str, sha: str, implements: list[str]):
    """Добавляет связи implements к уже зарегистрированному коммиту; повтор уже существующей связи пропускается."""
    commit = session.scalar(select(Artifact).where(Artifact.project_key == key, Artifact.type == "COMMIT",
                                                   Artifact.external_key == sha))
    if commit is None:
        raise DomainError(404, "COMMIT_NOT_FOUND", f"Коммит {sha} не зарегистрирован")
    have = {r.target_uid for r in session.scalars(select(Relation).where(
        Relation.project_key == key, Relation.type == "implements", Relation.source_uid == commit.uid))}
    added = []
    for d in implements:
        target = get_artifact(session, key, d)
        if target.type not in RELATION_RULES["implements"][1]:
            raise DomainError(422, "INVALID_RELATION", f"Коммит не может реализовывать {target.type}")
        if target.uid not in have:
            session.add(Relation(uid=new_uid(), project_key=key, type="implements", source_uid=commit.uid,
                                 target_uid=target.uid, status="confirmed", created_at=utcnow()))
            added.append(d)
    session.flush()
    return commit, added


# --- аналитика -----------------------------------------------------------------

def analyze(session: Session, key: str) -> Analysis:
    a = Analysis()
    # Связи читаются первыми: запросы идут без общего снимка, и связь, записанная параллельно между ними, иначе
    # ссылалась бы на ещё не прочитанный артефакт (KeyError → 500). Артефакт пишется раньше своих связей и вместе
    # с ревизией, поэтому всё, на что ссылаются прочитанные связи, попадёт в следующие запросы.
    relations = list(session.scalars(select(Relation).where(
        Relation.project_key == key, Relation.status == "confirmed").order_by(Relation.created_at)))
    arts = list(session.scalars(select(Artifact).where(Artifact.project_key == key)))
    a.by_uid = {x.uid: x for x in arts}
    if arts:
        revs = session.scalars(select(ArtifactRevision).where(
            ArtifactRevision.artifact_uid.in_(list(a.by_uid))).order_by(ArtifactRevision.number))
        for r in revs:  # по возрастанию номера: остаётся последняя
            a.revisions[r.artifact_uid] = r
    a.relations = relations
    commits_of: dict[str, list[Artifact]] = {}
    for r in a.relations:
        s, t = a.by_uid[r.source_uid], a.by_uid[r.target_uid]
        if r.type == "implements":
            a.implemented_by[t.uid] = s
            commits_of.setdefault(t.uid, []).append(s)
        elif r.type == "refines" and s.type == "FR" and t.type == "US":
            a.reflected.add(t.uid)
        elif r.type == "motivates" and s.type == "US" and t.type == "ADR":
            a.reflected.add(s.uid)
    a.unverified_only = {uid for uid, commits in commits_of.items()
                         if all(a.revisions[c.uid].attributes.get("sha_verified") is False for c in commits)}
    return a


def lost_items(session: Session, key: str, type_filter: str | None = None,
               priority: str | None = None, reason: str = "no_reflection") -> tuple[list[dict], list[str]]:
    """Возвращает элементы одного вида и display_id исключённых (обоснованное исключение).

    Виды разделены: no_reflection — потерянные (US без ФТ/ADR); no_code_link — не реализованы
    (FR/NFR/AC без implements). Не реализованное потерянным не считается.
    """
    get_project(session, key)
    a = analyze(session, key)
    items, excluded = [], []
    for art in a.by_uid.values():
        rev = a.revisions[art.uid]
        if rev.exemption_reason and art.type in REQUIREMENT_TYPES | {"US"}:
            excluded.append(art.display_id)
        reasons = a.reasons(art)
        if reason not in reasons or (type_filter and art.type != type_filter):
            continue
        if priority and rev.attributes.get("priority") != priority:
            continue
        last = a.implemented_by.get(art.uid)
        items.append({
            "display_id": art.display_id, "type": art.type, "title": rev.title,
            "priority": rev.attributes.get("priority"), "reasons": reasons,
            "last_commit": last.display_id if last else None,
            "recommended_action": "; ".join(RECOMMENDED[r] for r in reasons),
        })
    items.sort(key=lambda i: (i["priority"] or "P9", i["display_id"]))
    return items, sorted(excluded)


def graph(session, key, focus, depth, direction, limit, relation_types=None) -> dict:
    get_project(session, key)
    a = analyze(session, key)
    start = get_artifact(session, key, focus)
    out_adj, in_adj = {}, {}
    relations = [r for r in a.relations if not relation_types or r.type in relation_types]
    for r in relations:
        out_adj.setdefault(r.source_uid, []).append(r)
        in_adj.setdefault(r.target_uid, []).append(r)

    visited = {start.uid: 0}
    queue = deque([start.uid])
    truncated = False
    while queue:
        uid = queue.popleft()
        if visited[uid] >= depth:
            continue
        neighbours = []
        if direction in ("out", "both"):
            neighbours += [r.target_uid for r in out_adj.get(uid, [])]
        if direction in ("in", "both"):
            neighbours += [r.source_uid for r in in_adj.get(uid, [])]
        for nxt in neighbours:
            if nxt in visited:
                continue
            if len(visited) >= limit:
                truncated = True
                continue
            visited[nxt] = visited[uid] + 1
            queue.append(nxt)

    nodes = []
    for uid in visited:
        art = a.by_uid[uid]
        reasons = a.reasons(art)
        # Потерян — только без отражения в ФТ/ADR; без связи с кодом — отдельно «не реализован».
        nodes.append({"display_id": art.display_id, "type": art.type,
                      "title": a.revisions[uid].title, "lost": "no_reflection" in reasons,
                      "not_implemented": "no_code_link" in reasons, "reasons": reasons,
                      "level": visited[uid]})  # расстояние от центра в связях
    edges = [{"type": r.type, "source": a.by_uid[r.source_uid].display_id,
              "target": a.by_uid[r.target_uid].display_id}
             for r in relations if r.source_uid in visited and r.target_uid in visited]
    return {"focus": focus, "nodes": nodes, "edges": edges, "truncated": truncated}


# --- проверка качества требований (детерминированные правила, DEC-10) -----------

NUMBER = re.compile(r"\d")
VAGUE = re.compile(r"\b(быстро|удобно|легко|по возможности|при необходимости|как можно)\b", re.IGNORECASE)


# Пояснения правил замечаний: что проверяет правило, почему это важно и что сделать.
REVIEW_RULE_TEXT: dict[str, dict] = {
    "EXEMPTION_NO_REASON": {"severity": "error", "title": "Исключение без причины",
        "description": "Исключение снимает требование с проверки. Без причины непонятно, почему оно не проверяется. "
                       "Указать причину в поле исключения элемента."},
    "LOST_USER_STORY": {"severity": "error", "title": "User story без отражения",
        "description": "Проверяет user story: её не уточняет ни одна ФТ и она не привязана к ADR. Тогда неясно, как "
                       "её будут выполнять. Добавить ФТ со связью refines к этой US или ADR со связью motivates."},
    "NO_PRIORITY": {"severity": "warning", "title": "Не указан приоритет",
        "description": "Проверяет US, ФТ и НФТ: у них должен быть приоритет P0–P3. Без него элемент нельзя "
                       "упорядочить в плане и в списке потерянных. Указать приоритет в атрибутах элемента."},
    "NFR_NO_CRITERION": {"severity": "warning", "title": "НФТ без числового критерия",
        "description": "Проверяет НФТ: есть ли в формулировке число. Без числа требование нельзя проверить, "
                       "например «быстро» не проверяется, а «p95 ≤ 500 мс» проверяется. Пороги предлагает автор, "
                       "утверждает пользователь."},
    "AC_NOT_LINKED": {"severity": "warning", "title": "Приёмочный сценарий не связан",
        "description": "Проверяет AC: уточняет ли он какое-либо требование. Без связи непонятно, что сценарий проверяет. "
                       "Связать AC с ФТ или НФТ через refines."},
    "FR_NOT_LINKED": {"severity": "warning", "title": "ФТ не связана с user story",
        "description": "Проверяет ФТ: уточняет ли она хотя бы одну user story. Без этого неясна цель функции. "
                       "Связать ФТ с US через refines."},
    "DUPLICATE_TITLE": {"severity": "warning", "title": "Одинаковое название",
        "description": "Ищет элементы с одинаковым названием (без учёта регистра и пробелов). Их легко перепутать "
                       "в связях и обсуждениях. Переименовать один из них или объединить."},
    "DIAGRAM_NOT_LINKED": {"severity": "warning", "title": "Схема не привязана к процессу",
        "description": "Проверяет схемы: привязана ли схема к процессу, то есть к US или ФТ, которая его описывает. "
                       "Схема без процесса нельзя проверить на актуальность. Связать схему с US или ФТ связью illustrates."},
    "VAGUE_WORDING": {"severity": "info", "title": "Расплывчатая формулировка",
        "description": "Ищет слова без измеримого смысла: «быстро», «удобно», «легко», «по возможности», «при "
                       "необходимости», «как можно». Подсказка: заменить на условие, которое можно проверить."},
}


def review(session: Session, key: str) -> list[dict]:
    """Замечания по правилам, которые не требуют модели. Каждое — с ID, правилом и пояснением."""
    get_project(session, key)
    a = analyze(session, key)
    findings: list[dict] = []

    def add(severity: str, rule: str, art: Artifact, message: str) -> None:
        findings.append({"severity": severity, "rule": rule, "display_id": art.display_id, "message": message})

    titles: dict[str, list[Artifact]] = {}
    for art in sorted(a.by_uid.values(), key=lambda x: x.display_id):
        if art.type in {"COMMIT", "ADR"}:
            continue
        rev = a.revisions[art.uid]
        if rev.exemption_reason and not rev.exemption_reason.strip():
            add("error", "EXEMPTION_NO_REASON", art, "Исключение без пояснения")
        if art.type in {"US", "FR", "NFR"} and not rev.attributes.get("priority") and not rev.exemption_reason:
            add("warning", "NO_PRIORITY", art, "Не указан приоритет")
        if art.type == "NFR" and not NUMBER.search(f"{rev.title} {rev.text}"):
            add("warning", "NFR_NO_CRITERION", art, "Нефункциональное требование без числового критерия")
        if VAGUE.search(f"{rev.title} {rev.text}"):
            add("info", "VAGUE_WORDING", art, "Расплывчатая формулировка: " + VAGUE.search(f"{rev.title} {rev.text}").group(0))
        if art.type == "AC" and not any(r.type == "refines" and r.source_uid == art.uid for r in a.relations):
            add("warning", "AC_NOT_LINKED", art, "Приёмочный сценарий не связан с требованием")
        if art.type == "DIAGRAM" and not any(r.type == "illustrates" and r.source_uid == art.uid for r in a.relations):
            add("warning", "DIAGRAM_NOT_LINKED", art, "Схема не привязана ни к одному процессу (US или ФТ)")
        if art.type == "FR" and not any(r.type == "refines" and r.source_uid == art.uid for r in a.relations):
            add("warning", "FR_NOT_LINKED", art, "ФТ не уточняет ни одну user story")
        titles.setdefault(" ".join(rev.title.lower().split()), []).append(art)

    for group in titles.values():
        if len(group) > 1:
            for art in group:
                add("warning", "DUPLICATE_TITLE", art,
                    "Одинаковое название у: " + ", ".join(g.display_id for g in group if g.uid != art.uid))
    for item in lost_items(session, key, reason="no_reflection")[0]:
        if "no_reflection" in item["reasons"]:
            add("error", "LOST_USER_STORY", a.by_uid[next(u for u, x in a.by_uid.items() if x.display_id == item["display_id"])],
                "User story без ФТ и без ADR")
    return findings


# --- анализ влияния (§6.4): явные пути по каталогу связей ----------------------

# Названия и пояснения встроенных правил: их видят пользователь в UI и модель через API и скилл.
IMPACT_RULE_TEXT: dict[str, tuple[str, str]] = {
    "IMP-REFINES-CHILD": ("Уточняющее требование",
        "Родительская user story или ФТ задаёт цель, а уточняющие его ФТ и приёмочные критерии (AC) переводят её "
        "в конкретные требования. Когда родитель меняется, уточнения могут описывать уже не то, что нужно, поэтому "
        "автор каждого уточняющего требования проверяет его на соответствие новому тексту родителя. "
        "Правило явное: связь refines однозначно говорит, что уточнение зависит от родителя."),
    "IMP-REFINES-PARENT": ("Родительское требование",
        "Уточняющее требование (ФТ или AC) изменилось, и проверяется родитель: покрывает ли он теперь новое содержание "
        "или стал слишком узким. Это кандидат, а не нарушение: уточнение могло остаться в рамках родителя, и тогда "
        "ничего делать не нужно."),
    "IMP-IMPLEMENTS": ("Реализующий коммит",
        "Коммит объявляет, какие требования он реализует. Если требование изменилось, код, написанный под старый текст, "
        "может уже не соответствовать ему, поэтому коммит нужно перепроверить: закрывает ли тот же код новый текст. "
        "Сам коммит при этом не меняется, меняется смысл требования."),
    "IMP-IMPLEMENTED-REQ": ("Реализуемое требование",
        "Коммит изменился или зарегистрирован заново, и проверяется требование, которое он реализует: остаётся ли оно "
        "выполненным этим кодом. Это кандидат: правка коммита могла не менять поведение, и тогда требование в порядке."),
    "IMP-MOTIVATES": ("Опора на решение",
        "Требование опирается на ADR как на обоснование выбора. Если требование изменилось, ADR может перестать объяснять "
        "его, поэтому автор решения пересматривает, сохраняется ли основание. Сам ADR при этом не нарушен, "
        "он лишь нуждается в перепроверке."),
    "IMP-MOTIVATED-BY": ("Требование на решении",
        "ADR пересмотрен, и проверяются требования, которые на него опираются. Если решение изменилось, требования могут "
        "потерять обоснование или начать противоречить ему, поэтому автор каждого такого требования решает, "
        "нужно ли его менять."),
    "IMP-VERIFIES": ("Тест требования",
        "Тест по связи verifies доказывает, что требование выполняется. Если требование изменилось, тест может проверять "
        "уже другое поведение, поэтому его перечитывают и убеждаются, что он по-прежнему проверяет нужное и результат "
        "что-то значит."),
    "IMP-ILLUSTRATED-BY": ("Схема процесса",
        "Схема показывает процесс, который описывает US или ФТ. Когда описание процесса изменилось, схема может показывать "
        "уже другой порядок шагов, поэтому автор схемы сверяет её с новым текстом и обновляет."),
    "IMP-ILLUSTRATES": ("Описание процесса",
        "Схема изменилась, и проверяется US или ФТ, которую она иллюстрирует: совпадает ли текст с тем, что теперь нарисовано. "
        "Это кандидат: схему могли поправить только визуально."),
    "IMP-VERIFIES-TARGET": ("Покрытие тестом",
        "Тест изменился (переписан или перепривязан), и проверяется требование, которое он закрывает: осталось ли "
        "покрытие полным. Это кандидат: тест мог быть переименован без изменения проверки."),
}


# Направление: "out" — текущий элемент источник связи, "in" — цель. Правило срабатывает на соседа.
IMPACT_RULES = {
    # изменение родителя (US/FR) затрагивает уточняющие требования
    ("refines", "in"): ("IMP-REFINES-CHILD", "explicit", "проверить уточняющие требования"),
    # изменение уточняющего требования — кандидат для проверки покрытия родителя
    ("refines", "out"): ("IMP-REFINES-PARENT", "candidate", "проверить покрытие родительского требования"),
    # изменение требования затрагивает коммиты, которые его реализуют (требование — цель implements)
    ("implements", "in"): ("IMP-IMPLEMENTS", "explicit", "перепроверить реализацию коммитом"),
    # изменение коммита — кандидат для проверки требования, которое он реализует
    ("implements", "out"): ("IMP-IMPLEMENTED-REQ", "candidate", "проверить требование, которое реализует коммит"),
    # изменение требования затрагивает ADR, на которое оно опирается
    ("motivates", "out"): ("IMP-MOTIVATES", "explicit", "пересмотреть основание решения"),
    # изменение ADR затрагивает требования, которые на него опираются
    ("motivates", "in"): ("IMP-MOTIVATED-BY", "explicit", "проверить требования, опирающиеся на это решение"),
    # изменение теста затрагивает требование, которое он проверяет
    ("verifies", "out"): ("IMP-VERIFIES-TARGET", "candidate", "перепроверить покрытие требования тестом"),
    # изменение требования затрагивает тесты, которые его проверяют
    ("verifies", "in"): ("IMP-VERIFIES", "explicit", "перепроверить тест на соответствие"),
    # изменение процесса (US или ФТ) затрагивает схемы, которые его показывают
    ("illustrates", "in"): ("IMP-ILLUSTRATED-BY", "explicit", "актуализировать схему процесса"),
    # изменение схемы — кандидат для проверки описания процесса
    ("illustrates", "out"): ("IMP-ILLUSTRATES", "candidate", "сверить описание процесса со схемой"),
}


def _custom_rules(session: Session, key: str) -> list:
    from pth.models import ImpactRule
    return list(session.scalars(select(ImpactRule).where(ImpactRule.project_key == key, ImpactRule.enabled.is_(True))
                                .order_by(ImpactRule.code)))


def _stored_types(value: str | None) -> list[str] | None:
    """Типы из хранимой строки «FR,NFR»; None — любой допустимый по связи."""
    return value.split(",") if value else None


def _allows(stored: str | None, actual: str) -> bool:
    types = _stored_types(stored)
    return types is None or actual in types


def _matching_rules(rel_type: str, direction: str, focus_type: str, neighbor_type: str,
                    custom: list) -> list[tuple[str, str, str, str, str]]:
    """Все правила, которые затрагивают соседа: встроенное (если есть) и свои.
    Свое правило задает обе стороны: тип изменившегося (focus_type) и тип проверяемого соседа (neighbor_type).
    Кортеж: код, уверенность, действие, название, описание."""
    matched = []
    builtin = IMPACT_RULES.get((rel_type, direction))
    if builtin:
        code, certainty, action = builtin
        title, description = IMPACT_RULE_TEXT[code]
        matched.append((code, certainty, action, title, description))
    for rule in custom:
        if (rule.relation_type == rel_type and rule.direction == direction
                and _allows(rule.focus_type, focus_type) and _allows(rule.neighbor_type, neighbor_type)):
            matched.append((rule.code, rule.certainty, rule.action, rule.title, rule.description))
    return matched


TYPE_ORDER = ["US", "FR", "NFR", "AC", "ADR", "TEST", "COMMIT", "DIAGRAM"]


def _side_types(rel_type: str, direction: str) -> tuple[list[str], list[str]]:
    """Типы, которые может иметь изменившийся элемент (focus) и сосед, по каталогу связей.
    Направление «out»: изменившийся — источник связи; «in»: изменившийся — цель."""
    source, target = RELATION_RULES[rel_type]
    focus, neighbor = (source, target) if direction == "out" else (target, source)
    return [t for t in TYPE_ORDER if t in focus], [t for t in TYPE_ORDER if t in neighbor]


def _store_types(types: list[str], allowed: list[str], side: str) -> str | None:
    """Проверяет, что все выбранные типы допустимы для этой стороны связи, и возвращает строку для хранения."""
    bad = [t for t in types if t not in allowed]
    if bad:
        raise DomainError(422, "TYPE_NOT_ALLOWED",
                          f"Тип {', '.join(bad)} не может быть у {side} в этой связи; допустимо: {', '.join(allowed)}")
    chosen = [t for t in TYPE_ORDER if t in set(types)]
    return ",".join(chosen) or None


def _rule_out(rule) -> dict:
    focus_allowed, neighbor_allowed = _side_types(rule.relation_type, rule.direction)
    return {"code": rule.code, "title": rule.title, "description": rule.description,
            "relation_type": rule.relation_type, "direction": rule.direction,
            "focus_types": _stored_types(rule.focus_type) or focus_allowed,
            "neighbor_types": _stored_types(rule.neighbor_type) or neighbor_allowed,
            "certainty": rule.certainty, "action": rule.action, "enabled": rule.enabled}


def list_impact_rules(session: Session, key: str) -> dict:
    from pth.models import ImpactRule
    get_project(session, key)
    rows = session.scalars(select(ImpactRule).where(ImpactRule.project_key == key).order_by(ImpactRule.code)).all()
    builtin = []
    for (rel, d), (code, c, act) in IMPACT_RULES.items():
        focus_allowed, neighbor_allowed = _side_types(rel, d)
        builtin.append({"code": code, "title": IMPACT_RULE_TEXT[code][0], "description": IMPACT_RULE_TEXT[code][1],
                        "relation_type": rel, "direction": d, "focus_types": focus_allowed,
                        "neighbor_types": neighbor_allowed, "certainty": c, "action": act})
    return {"builtin": builtin, "custom": [_rule_out(r) for r in rows]}


def create_impact_rule(session: Session, key: str, data: dict) -> dict:
    from pth.models import ImpactRule
    get_project(session, key)
    if data["code"].startswith("IMP-"):
        raise DomainError(422, "RESERVED_RULE_CODE", "Префикс IMP- занят встроенными правилами")
    if session.scalar(select(ImpactRule).where(ImpactRule.project_key == key, ImpactRule.code == data["code"])):
        raise DomainError(409, "RULE_EXISTS", f"Правило {data['code']} уже есть")
    focus_allowed, neighbor_allowed = _side_types(data["relation_type"], data["direction"])
    rule = ImpactRule(uid=new_uid(), project_key=key, code=data["code"], title=data["title"].strip(),
                      description=data.get("description", "").strip(), relation_type=data["relation_type"],
                      direction=data["direction"],
                      focus_type=_store_types(data.get("focus_types") or [], focus_allowed, "изменившегося элемента"),
                      neighbor_type=_store_types(data.get("neighbor_types") or [], neighbor_allowed, "соседа"),
                      certainty=data["certainty"], action=data["action"].strip(), enabled=True, created_at=utcnow())
    session.add(rule)
    session.flush()
    return _rule_out(rule)


def _own_rule(session: Session, key: str, code: str):
    from pth.models import ImpactRule
    rule = session.scalar(select(ImpactRule).where(ImpactRule.project_key == key, ImpactRule.code == code))
    if rule is None:
        raise DomainError(404, "RULE_NOT_FOUND", f"Правила {code} нет в проекте {key}")
    return rule


def set_impact_rule_enabled(session: Session, key: str, code: str, enabled: bool) -> dict:
    rule = _own_rule(session, key, code)
    rule.enabled = enabled
    session.flush()
    return _rule_out(rule)


def delete_impact_rule(session: Session, key: str, code: str) -> None:
    session.delete(_own_rule(session, key, code))
    session.flush()


def impact(session: Session, key: str, focus: str, depth: int = 2, limit: int = 200) -> dict:
    """Обход от изменяемого элемента. Элемент попадает в результат, если его затрагивает хотя бы одно правило:
    встроенное по паре «связь + направление» или свой (включённый), с учётом типа соседа."""
    get_project(session, key)
    a = analyze(session, key)
    start = get_artifact(session, key, focus)
    custom = _custom_rules(session, key)
    edges_from: dict[str, list[tuple[Relation, str]]] = {}
    for r in a.relations:
        edges_from.setdefault(r.target_uid, []).append((r, "in"))   # кто ссылается на изменяемый
        edges_from.setdefault(r.source_uid, []).append((r, "out"))  # на что ссылается изменяемый
    visited = {start.uid: ()}
    items: list[dict] = []
    frontier = [start.uid]
    truncated = False
    for level in range(1, depth + 1):
        next_frontier = []
        for uid in frontier:
            for rel, direction in edges_from.get(uid, []):
                other = rel.source_uid if direction == "in" else rel.target_uid
                if other in visited or other == start.uid:
                    continue
                art = a.by_uid[other]
                matched = _matching_rules(rel.type, direction, a.by_uid[uid].type, art.type, custom)
                if not matched:
                    continue
                if len(items) >= limit:
                    truncated = True
                    break
                visited[other] = visited[uid] + (a.by_uid[uid].display_id,)
                items.append({"display_id": art.display_id, "type": art.type, "depth": level,
                              "path": [*visited[other], art.display_id],
                              "rule_id": matched[0][0], "rule_ids": [m[0] for m in matched],
                              "certainty": "explicit" if any(m[1] == "explicit" for m in matched) else "candidate",
                              "action": "; ".join(dict.fromkeys(m[2] for m in matched)),
                              "_matched": matched})
                next_frontier.append(other)
        frontier = next_frontier
    items.sort(key=lambda i: (i["depth"], i["display_id"]))
    # Пояснения правил, которые сработали в этом ответе: модель и UI читают их отсюда, а не из кода.
    rules = {}
    for item in items:
        for code in item["rule_ids"]:
            if code not in rules:
                title, description = next((m[3], m[4]) for m in item["_matched"] if m[0] == code)
                rules[code] = {"title": title, "description": description}
    for item in items:
        del item["_matched"]
    return {"focus": focus, "items": items, "rules": rules, "truncated": truncated,
            "note": "Явные пути по каталогу связей. Кандидаты не являются нарушениями."}


# --- проверки: свидетельства, соответствие, ПСИ, готовность (SK-10, SK-11, SK-13) ---


def record_evidence(session: Session, key: str, test_id: str, result: str,
                    commit_sha: str | None, environment: dict) -> "Evidence":
    from pth.models import Evidence
    test = get_artifact(session, key, test_id)
    if test.type != "TEST":
        raise DomainError(422, "NOT_A_TEST", f"{test_id} — не TEST")
    if result not in {"pass", "fail", "blocked", "inconclusive"}:
        raise DomainError(422, "INVALID_RESULT", "result: pass | fail | blocked | inconclusive")
    ev = Evidence(uid=new_uid(), project_key=key, test_uid=test.uid, commit_sha=commit_sha,
                  result=result, environment=environment, created_at=utcnow())
    session.add(ev)
    session.flush()
    return ev


def record_ci_results(session: Session, key: str, commit: str, oid: str | None, results: list[dict],
                      trust: str, issuer: str, environment: dict) -> list[dict]:
    """Пишет свидетельство на каждый тест из результата. verified — коммит найден в репозитории."""
    from pth.models import Evidence
    out = []
    for item in results:
        if not isinstance(item, dict):
            raise DomainError(422, "INVALID_RESULTS", "каждая запись — объект {test, result}")
        test = get_artifact(session, key, str(item.get("test", "")))
        if test.type != "TEST":
            raise DomainError(422, "NOT_A_TEST", f"{test.display_id} — не TEST")
        result = str(item.get("result", ""))
        if result not in {"pass", "fail", "blocked", "inconclusive"}:
            raise DomainError(422, "INVALID_RESULT", "result: pass | fail | blocked | inconclusive")
        ev = Evidence(uid=new_uid(), project_key=key, test_uid=test.uid, commit_sha=commit or None,
                      commit_oid=oid, verified=oid is not None, trust_level=trust, issuer=issuer,
                      result=result, environment=environment, created_at=utcnow())
        session.add(ev)
        out.append({"test": test.display_id, "result": result, "verified": ev.verified, "trust_level": trust,
                    "evidence_uid": ev.uid})
    session.flush()
    return out


MAX_ATTACHMENT = 25 * 1024 * 1024


def add_attachment(session: Session, key: str, evidence_uid: str, name: str, content_type: str, data: bytes) -> dict:
    import hashlib
    from pth.models import Evidence, EvidenceAttachment
    ev = session.get(Evidence, evidence_uid)
    if ev is None or ev.project_key != key:
        raise DomainError(404, "EVIDENCE_NOT_FOUND", f"Запуск {evidence_uid} не найден")
    name = Path(name or "").name.strip()
    if not name or len(name) > 200:
        raise DomainError(422, "INVALID_NAME", "Нужно имя файла до 200 знаков")
    if not data:
        raise DomainError(422, "EMPTY_ATTACHMENT", "Файл пустой")
    if len(data) > MAX_ATTACHMENT:
        raise DomainError(413, "ATTACHMENT_TOO_LARGE", f"Файл больше {MAX_ATTACHMENT // 1024 // 1024} МБ")
    att = EvidenceAttachment(uid=new_uid(), project_key=key, evidence_uid=evidence_uid, name=name,
                             content_type=(content_type or "application/octet-stream")[:100], size=len(data),
                             sha256=hashlib.sha256(data).hexdigest(), data=data, created_at=utcnow())
    session.add(att)
    session.flush()
    return attachment_out(att)


def attachment_out(att) -> dict:
    return {"uid": att.uid, "evidence_uid": att.evidence_uid, "name": att.name, "content_type": att.content_type,
            "size": att.size, "sha256": att.sha256, "created_at": att.created_at.isoformat()}


def allure_summary(data: bytes) -> dict | None:
    """Сводка из архива allure-results: число результатов по статусам, общее время, упавшие. None — это не allure."""
    import io, json, zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.endswith("-result.json")]
            if not names:
                return None
            counts: dict[str, int] = {}
            total_ms, failed = 0, []
            for n in names[:5000]:
                r = json.loads(z.read(n))
                st = r.get("status", "unknown")
                counts[st] = counts.get(st, 0) + 1
                total_ms += max(0, int(r.get("stop", 0)) - int(r.get("start", 0)))
                if st in ("failed", "broken") and len(failed) < 10:
                    failed.append(r.get("name") or r.get("fullName") or n)
    except (zipfile.BadZipFile, ValueError, KeyError):
        return None
    return {"total": sum(counts.values()), "counts": counts, "seconds": round(total_ms / 1000, 2), "failed": failed}


def sarif_summary(data: bytes) -> dict | None:
    """Сводка SARIF 2.1 (формат SonarQube-экспорта, Semgrep, Trivy, CodeQL, Bandit и др.): число находок по уровням,
    инструменты, самые частые правила. None — не SARIF."""
    import json
    try:
        doc = json.loads(data)
        runs = doc["runs"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(runs, list) or doc.get("version") is None and "$schema" not in doc:
        return None
    levels: dict[str, int] = {}
    rules: dict[str, int] = {}
    tools = []
    for run in runs:
        driver = (run.get("tool") or {}).get("driver") or {}
        tools.append(f"{driver.get('name', '?')} {driver.get('version', '')}".strip())
        for res in run.get("results") or []:
            lvl = res.get("level") or "warning"
            levels[lvl] = levels.get(lvl, 0) + 1
            rules[res.get("ruleId", "?")] = rules.get(res.get("ruleId", "?"), 0) + 1
    top = sorted(rules.items(), key=lambda x: -x[1])[:5]
    return {"tools": tools, "levels": levels, "total": sum(levels.values()), "top_rules": top}


def quality_gate_summary(data: bytes) -> dict | None:
    """Сводка quality gate SonarQube (ответ api/qualitygates/project_status): статус и невыполненные условия."""
    import json
    try:
        status = json.loads(data)["projectStatus"]
        return {"status": status["status"],
                "failed": [f"{c.get('metricKey')}: {c.get('actualValue')} (порог {c.get('errorThreshold')})"
                           for c in status.get("conditions", []) if c.get("status") == "ERROR"][:10]}
    except (ValueError, KeyError, TypeError):
        return None


def current_head(session: Session, key: str) -> str | None:
    """HEAD подключённого репозитория (читается сейчас). None — репозиторий не подключён или недоступен."""
    from pth import gitops
    from pth.models import ProjectRepo
    repo = session.get(ProjectRepo, key)
    if repo is None:
        return None
    try:
        return gitops.validate_repo(repo.path)["head"]
    except gitops.GitError:
        return None


def _test_history(repo, gitops, workdir: str | None, tokens: list[str]) -> dict | None:
    """История файлов теста в Git: где добавлен и менялся. None — репозиторий не подключён или команда без файлов."""
    if repo is None:
        return None
    specs = [x for x in tokens if x.endswith(".py") or "::" in x]
    if not specs:
        return None
    paths = [(f"{workdir}/" if workdir else "") + s.split("::")[0] for s in specs]
    pick = specs[0].split("::")[1] if "::" in specs[0] else None
    try:
        return gitops.file_history(repo.path, paths, pick)
    except gitops.GitError:
        return None


def latest_per_commit(rows: list) -> list:
    """Из запусков (новые первыми) оставляет по одному на пару «тест, коммит»: повтор на том же коммите не нужен."""
    seen, out = set(), []
    for e in rows:
        k = (e.test_uid, e.commit_oid or e.commit_sha)
        if k[1] is not None and k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def test_status(session: Session, key: str) -> dict:
    """Состояние каждого TEST: запускался ли, результат, на каком коммите, актуален ли для HEAD.
    Требования без теста — отдельным списком: тест не написан."""
    from pth.models import Evidence
    get_project(session, key)
    a = analyze(session, key)
    head = current_head(session, key)
    verifies: dict[str, list[str]] = {}
    for r in a.relations:
        if r.type == "verifies":
            verifies.setdefault(r.source_uid, []).append(a.by_uid[r.target_uid].display_id)
    implemented_by: dict[str, list[Artifact]] = {}
    for r in a.relations:
        if r.type == "implements":
            implemented_by.setdefault(a.by_uid[r.target_uid].display_id, []).append(a.by_uid[r.source_uid])
    from pth import gitops
    from pth.models import ProjectRepo
    repo = session.get(ProjectRepo, key)
    tests = []
    for art in sorted((x for x in a.by_uid.values() if x.type == "TEST"), key=lambda x: x.display_id):
        runs = latest_per_commit(session.scalars(select(Evidence).where(Evidence.test_uid == art.uid)
                                                 .order_by(Evidence.created_at.desc())).all())[:10]
        last = runs[0] if runs else None
        command = a.revisions[art.uid].attributes.get("command")
        if last is None:
            state, commit = "not_run", None
        else:
            commit = last.commit_oid or last.commit_sha
            if last.result == "fail":
                state = "failed"
            elif last.result != "pass":
                state = "inconclusive"
            elif not (last.verified or last.trust_level == "human_recorded"):
                state = "pending_verification"
            elif head is not None and commit and not head.startswith(commit) and commit != head:
                state = "passed_old_commit"
            else:
                state = "passed_current" if head is not None else "passed"
        coverage = a.revisions[art.uid].attributes.get("coverage", "full")
        reqs = sorted(verifies.get(art.uid, []))
        code = {c.display_id: c for r in reqs for c in implemented_by.get(r, [])}
        tokens = (command or "").replace("'", " ").split()
        selector = tokens[tokens.index("-k") + 1:] if "-k" in tokens else []
        tests.append({"test": art.display_id, "title": a.revisions[art.uid].title, "command": command,
                      "workdir": a.revisions[art.uid].attributes.get("workdir"),
                      "kind": a.revisions[art.uid].attributes.get("kind") if a.revisions[art.uid].attributes.get("kind") in CHECK_KINDS else "unit",
                      "artifacts": a.revisions[art.uid].attributes.get("artifacts"),
                      "text": a.revisions[art.uid].text,
                      "files": [x for x in tokens if x.endswith(".py") or "::" in x],
                      "selector": " ".join(selector) or None,
                      "test_history": _test_history(repo, gitops, a.revisions[art.uid].attributes.get("workdir"), tokens),
                      "requirement_titles": {r: a.revisions[next(u for u, x in a.by_uid.items() if x.display_id == r)].title for r in reqs},
                      "code_commits": [{"display_id": c.display_id, "sha": c.external_key, "title": a.revisions[c.uid].title}
                                       for c in code.values()],
                      "history": [{"result": e.result, "commit": e.commit_oid or e.commit_sha, "ran_at": e.created_at.isoformat(),
                                   "trust": e.trust_level, "verified": e.verified, "issuer": e.issuer} for e in runs],
                      "coverage": coverage, "coverage_gaps": a.revisions[art.uid].attributes.get("coverage_gaps"),
                      "requirements": sorted(verifies.get(art.uid, [])), "state": state,
                      "commit": commit, "result": last.result if last else None,
                      "ran_at": last.created_at.isoformat() if last else None})
    covered = {r for t in tests for r in t["requirements"]}
    untested = sorted(i.display_id for i in a.by_uid.values()
                      if i.type in VERIFIABLE_TYPES and i.display_id not in covered
                      and not a.revisions[i.uid].exemption_reason)
    counts: dict[str, int] = {}
    for t in tests:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    return {"head": head, "tests": tests, "counts": counts, "requirements_without_test": untested}


def compliance(session: Session, key: str, head_override: str | None = None) -> dict:
    """Для каждого FR/NFR/AC: verified | failed | not_verified | stale | no_test | excluded.

    verified — все тесты, связанные через verifies, имеют последнее свидетельство pass, и оно
    создано не раньше последней ревизии требования. Иначе — stale. Коммит и статус реализации
    здесь не учитываются: это отдельная проверка.
    """
    from pth.models import Evidence
    get_project(session, key)
    a = analyze(session, key)
    head = head_override or current_head(session, key)
    verifies: dict[str, list[Artifact]] = {}
    for r in a.relations:
        if r.type == "verifies":
            verifies.setdefault(r.target_uid, []).append(a.by_uid[r.source_uid])
    items = []
    for art in sorted(a.by_uid.values(), key=lambda x: x.display_id):
        if art.type not in VERIFIABLE_TYPES:
            continue
        rev = a.revisions[art.uid]
        if rev.exemption_reason:
            items.append({"display_id": art.display_id, "status": "excluded", "tests": []})
            continue
        tests = verifies.get(art.uid, [])
        if not tests:
            items.append({"display_id": art.display_id, "status": "no_test", "tests": []})
            continue
        statuses = []
        for test in tests:
            last = session.scalar(select(Evidence).where(Evidence.test_uid == test.uid)
                                  .order_by(Evidence.created_at.desc()).limit(1))
            if last is None:
                statuses.append((test.display_id, "not_verified"))
            elif not (last.verified or last.trust_level == "human_recorded"):
                # Свидетельство CI без проверки коммита в Git — «ожидает проверки», readiness его не считает (§13.2)
                statuses.append((test.display_id, "not_verified"))
            elif head is not None and last.commit_oid and last.commit_oid != head:
                # Свидетельство для другого коммита: для текущего кода не действует (§14.4)
                statuses.append((test.display_id, "stale"))
            elif last.result == "fail":
                statuses.append((test.display_id, "failed"))
            elif last.result != "pass":
                statuses.append((test.display_id, "not_verified"))
            elif _aware(last.created_at) < _aware(rev.created_at):
                statuses.append((test.display_id, "stale"))
            else:
                statuses.append((test.display_id, "verified"))
        values = {s for _, s in statuses}
        overall = next((s for s in ("failed", "stale", "not_verified") if s in values), None)
        if overall is None:
            partial = any(a.revisions[t.uid].attributes.get("coverage") == "partial" for t in tests)
            overall = "partial" if partial else "verified"  # неполный набор тестов не считается полной проверкой
        items.append({"display_id": art.display_id, "status": overall,
                      "tests": [{"display_id": t, "status": s} for t, s in statuses]})
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"counts": counts, "items": items}


def _aware(value):
    from datetime import timezone
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def readiness(session: Session, key: str) -> dict:
    """ready — все проверяемые требования (кроме исключённых) verified; иначе not_ready. Без данных — insufficient_data."""
    report = compliance(session, key)
    relevant = [i for i in report["items"] if i["status"] != "excluded"]
    if not relevant:
        verdict = "insufficient_data"
    elif all(i["status"] == "verified" for i in relevant):
        verdict = "ready"
    else:
        verdict = "not_ready"
    blocking = [{"display_id": i["display_id"], "status": i["status"]}
                for i in relevant if i["status"] != "verified"]
    return {"verdict": verdict, "total": len(relevant), "blocking": blocking, "counts": report["counts"]}


PYTEST_SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)")
PYTEST_TIME = re.compile(r"\bin ([\d.]+)s")
RESULT_RU = {"pass": "пройден", "fail": "упал", "blocked": "не удалось запустить", "inconclusive": "не определён"}
STATUS_RU = {"verified": "проверено", "partial": "покрыто частично", "failed": "упало", "stale": "устарело",
             "not_verified": "не проверено", "no_test": "нет теста"}


def parse_run_output(environment: dict) -> dict:
    """Числа из вывода запуска: pytest-итог (passed/failed/…), время, код выхода. Только то, что реально есть в логе."""
    log = (environment or {}).get("log_tail") or ""
    counts: dict[str, int] = {}
    for line in reversed(log.splitlines()):
        found = PYTEST_SUMMARY.findall(line)
        if found and PYTEST_TIME.search(line):
            counts = {name.rstrip("s") if name.startswith("error") else name: int(n) for n, name in found}
            counts["seconds"] = float(PYTEST_TIME.search(line).group(1))
            break
    return {"counts": counts, "exit_code": (environment or {}).get("exit_code"),
            "command": (environment or {}).get("command")}


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


CHECK_KINDS = {"unit": "функциональные тесты", "load": "нагрузочные испытания",
               "static": "статический анализ кода (SonarQube и аналоги)", "security": "поиск уязвимостей и зависимостей",
               "other": "прочие проверки CI"}
TEXT_EXT = (".log", ".txt", ".md", ".csv", ".json", ".out")
EMBED_LIMIT = 3000


def _size(n: int) -> str:
    return f"{n} Б" if n < 1024 else f"{n / 1024:.1f} КБ" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} МБ"


def _attachment_lines(session: Session, key: str, run) -> list[str]:
    """Приложения последнего запуска: ссылка на файл, хеш, для Allure-архива сводка, для текстовых — начало содержимого."""
    from pth.models import EvidenceAttachment
    atts = session.scalars(select(EvidenceAttachment).where(EvidenceAttachment.evidence_uid == run.uid)
                           .order_by(EvidenceAttachment.created_at)).all()
    if not atts:
        return ["- **Приложения:** нет (ни полного лога, ни отчётов)"]
    lines = ["- **Приложения к запуску:**"]
    extra: list[str] = []
    for att in atts:
        lines.append(f"  - [{att.name}](/v1/projects/{key}/attachments/{att.uid}) — {_size(att.size)}, sha256 `{att.sha256[:12]}`")
        lower = att.name.lower()
        summary = allure_summary(att.data) if lower.endswith(".zip") else None
        if summary:
            parts = ", ".join(f"{n} {st}" for st, n in sorted(summary["counts"].items()))
            extra.append(f"  - Allure `{att.name}`: всего {summary['total']} ({parts}); суммарное время {summary['seconds']} с")
            extra += [f"    - не пройден: {f}" for f in summary["failed"]]
        if lower.endswith((".sarif", ".json")):
            sarif = sarif_summary(att.data)
            gate = None if sarif else quality_gate_summary(att.data)
            if sarif:
                parts = ", ".join(f"{n} {lvl}" for lvl, n in sorted(sarif["levels"].items())) or "находок нет"
                extra.append(f"  - Статический анализ `{att.name}` ({'; '.join(sarif['tools'])}): {sarif['total']} находок ({parts})")
                extra += [f"    - правило `{r}`: {n}" for r, n in sarif["top_rules"]]
            if gate:
                extra.append(f"  - Quality gate `{att.name}`: {gate['status']}")
                extra += [f"    - не выполнено: {f}" for f in gate["failed"]]
    lines += extra
    for att in atts:
        if att.name.lower().endswith(TEXT_EXT) and sarif_summary(att.data) is None and quality_gate_summary(att.data) is None:
            text = att.data.decode("utf-8", errors="replace")
            shown = text[-EMBED_LIMIT:] if att.name.lower().endswith((".log", ".out", ".txt")) else text[:EMBED_LIMIT]
            cut = len(text) > EMBED_LIMIT
            lines += ["", f"  Фрагмент `{att.name}`{' (' + ('конец' if att.name.lower().endswith((chr(46) + 'log', chr(46) + 'out', chr(46) + 'txt')) else 'начало') + ', полный файл по ссылке)' if cut else ''}:",
                      "", "  ```text", *["  " + ln for ln in shown.splitlines()], "  ```"]
    return lines


def psi_markdown(session: Session, key: str) -> str:
    """Черновик ПСИ: сводка и вердикт, что и как проверено тестами, результаты запусков с числами из вывода,
    условия проверки из текста требований, пробелы с причиной. Числа берутся только из реальных данных."""
    from pth import gitops
    from pth.models import Evidence, ProjectRepo
    report = compliance(session, key)
    ready = readiness(session, key)
    status = test_status(session, key)
    a = analyze(session, key)
    by_display = {x.display_id: x for x in a.by_uid.values()}
    last_run: dict[str, "Evidence"] = {}
    for e in session.scalars(select(Evidence).where(Evidence.project_key == key).order_by(Evidence.created_at.desc())):
        last_run.setdefault(e.test_uid, e)

    repo = session.get(ProjectRepo, key)
    lines = [f"# Программа и методика испытаний — {key} (черновик)", "",
             f"Сформировано: {utcnow().strftime('%d.%m.%Y %H:%M')} UTC. "
             f"HEAD репозитория: `{(status['head'] or 'не подключён')[:10]}`. "
             "Документ не утверждён: он собран из требований и свидетельств проверок. "
             "Числа и условия взяты из текста требований и вывода запусков, ничего не придумано.", ""]

    lines += ["## Сводка", "",
              f"**Вердикт готовности:** {ready['verdict']} (блокирующих элементов: {len(ready['blocking'])} из {ready['total']}).", "",
              "| Статус требований | Количество |", "| --- | --- |"]
    for st, n in sorted(report["counts"].items(), key=lambda x: -x[1]):
        lines.append(f"| {STATUS_RU.get(st, st)} | {n} |")
    lines += ["", "| Состояние тестов | Количество |", "| --- | --- |"]
    for st, n in sorted(status["counts"].items(), key=lambda x: -x[1]):
        lines.append(f"| {st} | {n} |")
    if not status["counts"]:
        lines.append("| тестов нет | 0 |")

    lines += ["", "## Тесты и результаты запусков", ""]
    if not status["tests"]:
        lines += ["Тестов нет: нечего показать.", ""]
    ordered = sorted(status["tests"], key=lambda x: (list(CHECK_KINDS).index(x["kind"]), x["test"]))
    current_kind = None
    for tst in ordered:
        if tst["kind"] != current_kind:
            current_kind = tst["kind"]
            lines += [f"## Вид проверки: {CHECK_KINDS[current_kind]}", ""]
        run = last_run.get(by_display[tst["test"]].uid)
        lines += [f"### {tst['test']} — {tst['title']}", ""]
        lines.append(f"- **Что запускается:** `{tst['command'] or 'команда не задана'}`"
                     + (f", папка `{tst['workdir']}`" if tst.get("workdir") else ""))
        if tst["files"]:
            lines.append(f"- **Файлы:** {', '.join('`' + f + '`' for f in tst['files'])}"
                         + (f"; отбор `-k {tst['selector']}`" if tst["selector"] else ""))
        lines.append("- **Проверяемые требования:** " + (", ".join(f"{r}" for r in tst["requirements"]) or "нет"))
        if tst["coverage"] == "partial":
            lines.append(f"- **Покрытие неполное:** {tst['coverage_gaps'] or 'причина не указана'}")
        if run is None:
            lines += ["- **Запусков нет.**", ""]
            continue
        out = parse_run_output(run.environment)
        c = out["counts"]
        lines.append(f"- **Последний запуск:** {RESULT_RU.get(run.result, run.result)}, "
                     f"{run.created_at.strftime('%d.%m.%Y %H:%M')}, коммит `{(run.commit_oid or run.commit_sha or '—')[:10]}`, "
                     f"источник: {'хук CI' if run.trust_level == 'trusted_ci' else 'внесено вручную'}"
                     + ("" if run.verified or run.trust_level != "trusted_ci" else ", коммит не подтверждён в Git"))
        if c:
            parts = [f"{c[k]} {name}" for k, name in (("passed", "прошло"), ("failed", "упало"), ("error", "ошибок"),
                                                       ("skipped", "пропущено"), ("deselected", "не выбрано отбором"),
                                                       ("xfailed", "ожидаемо упало")) if k in c]
            lines.append(f"- **Результат запуска:** {', '.join(parts)}; время {c['seconds']} с; код выхода {out['exit_code']}")
        elif out["exit_code"] is not None:
            lines.append(f"- **Результат запуска:** вывод без итоговой строки pytest; код выхода {out['exit_code']}")
        else:
            lines.append("- **Результат запуска:** измеренных значений нет (результат внесён без вывода)")
        lines += _attachment_lines(session, key, run)
        lines.append("")

    lines += ["## Проверки требований", "",
              "| Требование | Название | Условие проверки | Тесты | Статус |", "| --- | --- | --- | --- | --- |"]
    for item in report["items"]:
        if item["status"] == "excluded":
            continue
        art = by_display[item["display_id"]]
        rev = a.revisions[art.uid]
        condition = rev.text.strip() or "(в тексте требования условие не сформулировано)"
        if len(condition) > 160:
            condition = condition[:157] + "…"
        tests = ", ".join(f"{x['display_id']} ({STATUS_RU.get(x['status'], x['status'])})" for x in item["tests"]) or "—"
        lines.append(f"| {item['display_id']} | {_cell(rev.title)} | {_cell(condition)} | {tests} | {STATUS_RU.get(item['status'], item['status'])} |")

    reasons = {"no_test": "тест не написан или не связан (verifies)", "not_verified": "тест не запускался или результат не подтверждён",
               "stale": "требование или коммит изменились после последнего запуска", "failed": "последний запуск упал",
               "partial": "набор тестов неполный"}
    gaps = [i for i in report["items"] if i["status"] in reasons]
    lines += ["", "## Пробелы", ""]
    lines += [f"- {g['display_id']}: {reasons[g['status']]}" for g in gaps] or ["- нет"]
    lines += ["", "## Ограничения отчёта", "",
              "- Измеренные значения (время ответа, нагрузка, p95 и т.п.) попадают сюда только если тест вывел их в лог. "
              "Сейчас хук сохраняет итог pytest и конец лога, поэтому для требований с числовыми порогами нужен тест, который эти числа печатает.",
              "- Статус «проверено» значит, что свидетельство новее последней правки требования и относится к текущему HEAD; "
              "это не заключение о соответствии функции."]
    return "\n".join(lines) + "\n"


def create_baseline(session: Session, key: str, name: str) -> "Baseline":
    from pth.models import Baseline
    a = analyze(session, key)
    snapshot = {
        "artifacts": {art.display_id: a.revisions[art.uid].number for art in a.by_uid.values()},
        "relations": sorted([r.type, a.by_uid[r.source_uid].display_id, a.by_uid[r.target_uid].display_id]
                            for r in a.relations),
        "taken_at": utcnow().isoformat(),
    }
    base = Baseline(uid=new_uid(), project_key=key, name=name, snapshot=snapshot, created_at=utcnow())
    session.add(base)
    session.flush()
    return base


# --- аудит и решения по замечаниям ----------------------------------------------

def audit(session: Session, key: str, actor: str, action: str, target: str | None = None,
          details: dict | None = None) -> None:
    """Добавляет запись аудита в ту же транзакцию, что и изменение (NFR-009)."""
    from pth.models import AuditEvent
    session.add(AuditEvent(uid=new_uid(), project_key=key, actor=actor or "unknown", action=action,
                           target=target, details=details or {}, created_at=utcnow()))


def decide_review(session: Session, key: str, rule: str, display_id: str, state: str, reason: str,
                  actor: str) -> "ReviewDecision":
    from pth.models import ReviewDecision
    if state not in {"resolved", "dismissed"}:
        raise DomainError(422, "INVALID_STATE", "state: resolved | dismissed")
    if not reason.strip():
        raise DomainError(422, "REASON_REQUIRED", "Решение без причины не принимается")
    artifact = get_artifact(session, key, display_id)
    rev = latest_revision(session, artifact).number
    decision = ReviewDecision(uid=new_uid(), project_key=key, rule=rule, display_id=display_id,
                              revision=rev, state=state, reason=reason.strip(), actor=actor,
                              created_at=utcnow())
    session.add(decision)
    audit(session, key, actor, "review.decide", f"{rule}:{display_id}", {"state": state, "revision": rev})
    session.flush()
    return decision


def review_state(session: Session, key: str, findings: list[dict]) -> list[dict]:
    """Добавляет к каждому замечанию состояние: open | resolved | dismissed | reopened."""
    from pth.models import ReviewDecision
    arts = {a.display_id: a for a in session.scalars(select(Artifact).where(Artifact.project_key == key))}
    decisions = session.scalars(select(ReviewDecision).where(ReviewDecision.project_key == key)
                                .order_by(ReviewDecision.created_at)).all()
    last: dict[tuple[str, str], ReviewDecision] = {}
    for d in decisions:
        last[(d.rule, d.display_id)] = d
    out = []
    for f in findings:
        d = last.get((f["rule"], f["display_id"]))
        state = "open"
        if d is not None:
            current = domain_latest_number(session, arts.get(f["display_id"]))
            if current != d.revision:
                state = "reopened"  # элемент изменился после решения
            else:
                state = d.state
        out.append({**f, "state": state, "decision": None if d is None else {
            "state": d.state, "reason": d.reason, "actor": d.actor, "revision": d.revision}})
    return out


def domain_latest_number(session: Session, artifact) -> int | None:
    if artifact is None:
        return None
    return latest_revision(session, artifact).number


def artifacts_page(session: Session, key: str, type_: str | None, offset: int, limit: int) -> tuple[list, int]:
    query = select(Artifact).where(Artifact.project_key == key)
    if type_:
        query = query.where(Artifact.type == type_)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(query.order_by(Artifact.display_id).offset(offset).limit(limit)).all()
    return rows, total


def compare_baselines(session: Session, key: str, a_uid: str, b_uid: str) -> dict:
    from pth.models import Baseline
    a = session.scalar(select(Baseline).where(Baseline.project_key == key, Baseline.uid == a_uid))
    b = session.scalar(select(Baseline).where(Baseline.project_key == key, Baseline.uid == b_uid))
    if a is None or b is None:
        raise DomainError(404, "BASELINE_NOT_FOUND", "Один из снимков не найден")
    sa, sb = a.snapshot["artifacts"], b.snapshot["artifacts"]
    ra, rb = {tuple(x) for x in a.snapshot["relations"]}, {tuple(x) for x in b.snapshot["relations"]}
    return {
        "a": a.name, "b": b.name,
        "added": sorted(set(sb) - set(sa)),
        "removed": sorted(set(sa) - set(sb)),
        "changed": sorted(k for k in set(sa) & set(sb) if sa[k] != sb[k]),
        "relations_added": [list(x) for x in sorted(rb - ra)],
        "relations_removed": [list(x) for x in sorted(ra - rb)],
    }
