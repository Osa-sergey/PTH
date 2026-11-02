"""HTTP-слой: маршруты /v1, обработка доменных ошибок и фабрика приложения."""
import os
from urllib.parse import quote
from collections.abc import Iterator

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pth import auth, domain, importer, jobs, llm, releases
from pth.db import Base, make_engine, make_session_factory
from pth.models import (
    Artifact, ArtifactRevision, ChangeSet, ExtractionJob, ImportSession, Project, Relation, new_uid, utcnow,
)
from pth.schemas import (
    ArtifactIn, ArtifactOut, ChangeSetIn, ChangeSetOut, CommitIn, Direction, GraphOut, ImportDetail,
    ImpactRuleIn, ImpactRulePatch, CommitLinkIn, ReleaseIn, ReleaseItemsIn, ReleaseReasonIn, ReleaseDoIn, ReleaseMoveIn, ImportPreviewIn, DraftIn,
    ImportIn, ImportReport, LostOut, ProjectIn, ProjectOut, RelationIn, RelationOut, RevisionIn,
    RevisionOut, ExtractIn, ExtractOut, Proposal, ProposalsToChangeSetIn,
    ExtractionJobIn, ExtractionJobOut,
)

DEFAULT_DB = "sqlite:///./pth.db"
GRAPH_LIMIT_MAX = 200

def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def guard(request: Request, session: Session = Depends(get_session)) -> None:
    """Общая проверка для всех /v1: чтение требует reader, запись — writer, без ключа проекта — admin."""
    principal = auth.authenticate(session, request.headers.get("authorization"))
    request.state.actor = principal.name
    request.state.principal_kind = principal.kind
    if request.url.path == "/v1/me":
        return  # любой действующий ключ может узнать свои права
    key = request.path_params.get("key")
    need = "reader" if request.method == "GET" else "writer"
    auth.authorize(principal, key, need)


router = APIRouter(prefix="/v1", tags=["PTH"], dependencies=[Depends(guard)])


def _page(response: Response, items: list, offset: int, limit: int) -> list:
    """Страница списка; общее число — в заголовке X-Total-Count (тело остаётся списком)."""
    response.headers["X-Total-Count"] = str(len(items))
    return items[offset:offset + limit]


def _artifact_out(session: Session, artifact: Artifact) -> ArtifactOut:
    rev = domain.latest_revision(session, artifact)
    return ArtifactOut(
        display_id=artifact.display_id, type=artifact.type, external_key=artifact.external_key,
        revision=rev.number, title=rev.title, text=rev.text, attributes=rev.attributes,
        exemption_reason=rev.exemption_reason, created_at=artifact.created_at,
    )


def _relation_out(session: Session, relation: Relation) -> RelationOut:
    return RelationOut(
        uid=relation.uid, type=relation.type, status=relation.status,
        source=session.get(Artifact, relation.source_uid).display_id,
        target=session.get(Artifact, relation.target_uid).display_id,
    )


# --- проекты -------------------------------------------------------------------

@router.post("/projects", status_code=201, response_model=ProjectOut, summary="Создать проект (только admin)")
def create_project(body: ProjectIn, session: Session = Depends(get_session)):
    project = domain.create_project(session, body.key, body.name)
    session.commit()
    return ProjectOut(key=project.key, name=project.name, created_at=project.created_at)


@router.get("/projects", response_model=list[ProjectOut], summary="Список проектов (только admin)")
def list_projects(session: Session = Depends(get_session)):
    return [ProjectOut(key=p.key, name=p.name, created_at=p.created_at)
            for p in session.scalars(select(Project).order_by(Project.key))]


@router.get("/projects/{key}", response_model=ProjectOut, summary="Проект по ключу")
def get_project(key: str, session: Session = Depends(get_session)):
    p = domain.get_project(session, key)
    return ProjectOut(key=p.key, name=p.name, created_at=p.created_at)


# --- артефакты и ревизии -------------------------------------------------------

@router.post("/projects/{key}/artifacts", status_code=201, response_model=ArtifactOut,
             summary="Создать артефакт (первая ревизия)")
def create_artifact(key: str, body: ArtifactIn, request: Request, session: Session = Depends(get_session)):
    artifact = domain.create_artifact(session, key, body.type, body.title, body.text,
                                      body.attributes, body.exemption_reason)
    domain.audit(session, key, request.state.actor, "artifact.create", artifact.display_id, {"type": body.type})
    session.commit()
    return _artifact_out(session, artifact)


@router.get("/projects/{key}/artifacts", response_model=list[ArtifactOut],
            summary="Список артефактов проекта (страницами; общее число — в X-Total-Count)")
def list_artifacts(response: Response, key: str, type: str | None = Query(default=None),
                   offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=500),
                   session: Session = Depends(get_session)):
    domain.get_project(session, key)
    rows, total = domain.artifacts_page(session, key, type, offset, limit)
    response.headers["X-Total-Count"] = str(total)
    return [_artifact_out(session, a) for a in rows]


@router.get("/projects/{key}/artifacts/{display_id}", response_model=ArtifactOut,
            summary="Артефакт с текущей ревизией")
def get_artifact(key: str, display_id: str, session: Session = Depends(get_session)):
    return _artifact_out(session, domain.get_artifact(session, key, display_id))


@router.post("/projects/{key}/artifacts/{display_id}/revisions", status_code=201,
             response_model=ArtifactOut, summary="Новая ревизия (старые не меняются)")
def add_revision(key: str, display_id: str, body: RevisionIn, request: Request, session: Session = Depends(get_session)):
    artifact, rev = domain.add_revision(session, key, display_id, body.title, body.text,
                                        body.attributes, body.exemption_reason, body.expected_revision)
    domain.audit(session, key, request.state.actor, "artifact.revise", display_id, {"revision": rev.number})
    session.commit()
    return _artifact_out(session, artifact)


@router.get("/projects/{key}/artifacts/{display_id}/revisions", response_model=list[RevisionOut],
            summary="История ревизий")
def list_revisions(key: str, display_id: str, session: Session = Depends(get_session)):
    artifact = domain.get_artifact(session, key, display_id)
    revs = session.scalars(select(ArtifactRevision).where(
        ArtifactRevision.artifact_uid == artifact.uid).order_by(ArtifactRevision.number))
    return [RevisionOut(number=r.number, title=r.title, text=r.text, attributes=r.attributes,
                        exemption_reason=r.exemption_reason, created_at=r.created_at) for r in revs]


# --- связи и коммиты -----------------------------------------------------------

@router.post("/projects/{key}/relations", status_code=201, response_model=RelationOut,
             summary="Создать связь по каталогу §6.1")
def create_relation(key: str, body: RelationIn, request: Request, session: Session = Depends(get_session)):
    relation = domain.create_relation(session, key, body.type, body.source, body.target)
    domain.audit(session, key, request.state.actor, "relation.create", f"{body.source}->{body.target}", {"type": body.type})
    session.commit()
    return _relation_out(session, relation)


@router.get("/projects/{key}/relations", response_model=list[RelationOut], summary="Связи проекта")
def list_relations(key: str, session: Session = Depends(get_session)):
    domain.get_project(session, key)
    rels = session.scalars(select(Relation).where(Relation.project_key == key)
                           .order_by(Relation.created_at))
    return [_relation_out(session, r) for r in rels]


@router.post("/projects/{key}/commits", status_code=201, response_model=ArtifactOut,
             summary="Зарегистрировать коммит и связать его с требованиями (implements)")
def create_commit(key: str, body: CommitIn, request: Request, session: Session = Depends(get_session)):
    commit = domain.create_commit(session, key, body.sha, body.title, body.implements)
    domain.audit(session, key, request.state.actor, "commit.register", body.sha, {"implements": body.implements})
    session.commit()
    return _artifact_out(session, commit)


# --- импорт принятой спецификации ----------------------------------------------

@router.post("/projects/{key}/imports", status_code=201, response_model=ImportReport,
             summary="Импорт принятой спецификации (идемпотентен по ID и тексту)")
def import_spec(
    key: str,
    body: ImportIn,
    request: Request,
    dry_run: bool = Query(default=False, description="Только предпросмотр: данные не меняются"),
    as_changeset: bool = Query(default=False, description="Сохранить план черновиком ChangeSet, данные не менять"),
    session: Session = Depends(get_session),
):
    if dry_run and as_changeset:
        raise domain.DomainError(422, "INVALID_MODE", "dry_run и as_changeset взаимоисключающи")
    try:
        report = importer.apply_import(session, key, body.source_name, body.text, dry_run, as_changeset)
        if not dry_run:
            domain.audit(session, key, request.state.actor, "import.apply" if not as_changeset else "import.changeset",
                         body.source_name, {"created": report["created"], "revised": report["revised"]})
            session.commit()
    except domain.DomainError:
        session.rollback()
        raise
    return ImportReport(**report)


@router.get("/projects/{key}/imports", response_model=list[ImportReport], summary="Журнал импортов")
def list_imports(key: str, response: Response, offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=20, ge=1, le=500), session: Session = Depends(get_session)):
    domain.get_project(session, key)
    records = session.scalars(select(ImportSession).where(ImportSession.project_key == key)
                              .order_by(ImportSession.created_at))
    return _page(response, [ImportReport(**importer.report_of(r)) for r in records], offset, limit)


# --- выделение требований моделью (LM Studio) ----------------------------------

def _llm(request: Request) -> llm.LLMClient:
    state = request.app.state
    return llm.LLMClient(state.llm_base, state.llm_key, transport=state.llm_transport)


@router.get("/llm/models", response_model=list[str],
            summary="Модели LM Studio (только admin: проекта здесь нет)")
def llm_models(request: Request):
    return _llm(request).models()


@router.post("/projects/{key}/extractions", response_model=ExtractOut,
             summary="Выделить требования из текста моделью. Ничего не записывает.")
def extract(key: str, body: ExtractIn, request: Request, session: Session = Depends(get_session)):
    domain.get_project(session, key)
    raw = _llm(request).extract(body.model, body.text)
    proposals = llm.validate(raw, body.text, body.model)
    return ExtractOut(model=body.model, proposals=[Proposal(**p) for p in proposals],
                      skipped=len(raw) - len(proposals))


def _job_out(job: ExtractionJob) -> ExtractionJobOut:
    return ExtractionJobOut(
        uid=job.uid, state=job.state, model=job.model, source_name=job.source_name,
        chunks_total=job.chunks_total, chunks_done=job.chunks_done,
        proposals=[Proposal(**p) for p in job.proposals], skipped=job.skipped, errors=job.errors,
        created_at=job.created_at, finished_at=job.finished_at,
    )


@router.post("/projects/{key}/extractions/jobs", status_code=202, response_model=ExtractionJobOut,
             summary="Выделить требования по всему документу в фоне (по фрагментам)")
def start_extraction_job(key: str, body: ExtractionJobIn, request: Request, session: Session = Depends(get_session)):
    domain.get_project(session, key)
    job = ExtractionJob(uid=new_uid(), project_key=key, model=body.model, source_name=body.source_name,
                        state="queued", created_at=utcnow())
    session.add(job)
    session.commit()
    state = request.app.state  # фоновая задача не держит объект запроса
    make_client = lambda: llm.LLMClient(state.llm_base, state.llm_key, transport=state.llm_transport)  # noqa: E731
    jobs.start(state.session_factory, make_client, job.uid, body.text)
    return _job_out(job)


@router.get("/projects/{key}/extractions/jobs/{uid}", response_model=ExtractionJobOut,
            summary="Прогресс и результат фонового выделения")
def get_extraction_job(key: str, uid: str, session: Session = Depends(get_session)):
    job = session.scalar(select(ExtractionJob).where(ExtractionJob.project_key == key, ExtractionJob.uid == uid))
    if job is None:
        raise domain.DomainError(404, "JOB_NOT_FOUND", f"Задача {uid} не найдена")
    session.refresh(job)
    return _job_out(job)


@router.post("/projects/{key}/extractions/changesets", status_code=201, response_model=ChangeSetOut,
             summary="Предложения модели → черновик ChangeSet (применяется отдельно)")
def proposals_to_changeset(key: str, body: ProposalsToChangeSetIn, session: Session = Depends(get_session)):
    domain.get_project(session, key)
    operations = []
    next_number: dict[str, int] = {}
    for p in body.proposals:
        if p.type not in next_number:
            current = session.scalar(select(Artifact.number).where(
                Artifact.project_key == key, Artifact.type == p.type)
                .order_by(Artifact.number.desc()).limit(1))
            next_number[p.type] = (current or 0) + 1
        number = next_number[p.type]
        next_number[p.type] += 1
        operations.append({
            "op": "create_artifact", "display_id": f"{key}-{p.type}-{number:03d}", "type": p.type,
            "number": number, "title": p.title, "text": p.text,
            "attributes": {"provenance": {"source": "llm", "model": p.model, "quote": p.quote,
                                          "quote_verified": p.quote_verified}},
        })
    cs = ChangeSet(uid=new_uid(), project_key=key, title=body.title, state="draft",
                   operations=operations, created_at=utcnow())
    if body.source is not None:
        # Источник сохранится как импорт при применении черновика: цитаты модели привязываются к строкам текста.
        import hashlib
        items = [{"display_id": op["display_id"], "quote": op["attributes"]["provenance"]["quote"],
                  "verified": op["attributes"]["provenance"]["quote_verified"]} for op in operations]
        fragments, missing = importer.proposal_fragments(body.source.text, items)
        cs.source = {"name": body.source.name, "text": body.source.text,
                     "sha256": hashlib.sha256(body.source.text.encode("utf-8")).hexdigest(),
                     "report": {"fragments": fragments, "created": len(operations), "revised": 0, "unchanged": 0,
                                "relations_created": 0, "unparsed": [], "changes": [
                                    {"display_id": op["display_id"], "action": "created"} for op in operations],
                                "via": "llm", "quotes_not_found": missing}}
    session.add(cs)
    session.commit()
    return _changeset_out(cs)


# --- черновики изменений (ChangeSet) --------------------------------------------

def _changeset_out(cs: ChangeSet) -> ChangeSetOut:
    return ChangeSetOut(uid=cs.uid, title=cs.title, state=cs.state, operations=cs.operations,
                        operation_count=len(cs.operations), last_error=cs.last_error,
                        created_at=cs.created_at, applied_at=cs.applied_at)


def _get_changeset(session: Session, key: str, uid: str) -> ChangeSet:
    cs = session.scalar(select(ChangeSet).where(ChangeSet.project_key == key, ChangeSet.uid == uid))
    if cs is None:
        raise domain.DomainError(404, "CHANGESET_NOT_FOUND", f"ChangeSet {uid} не найден")
    return cs


def stamp_operations(session: Session, key: str, operations: list[dict]) -> list[dict]:
    """Номер ревизии ставит сервер: в add_revision клиентский expected_revision отбрасывается и заменяется
    текущей ревизией элемента на момент создания черновика. Конфликт потом ловится при применении."""
    out = []
    for op in operations:
        if isinstance(op, dict) and op.get("op") == "add_revision":
            artifact = domain.get_artifact(session, key, str(op.get("display_id", "")))
            op = {**op, "expected_revision": domain.latest_revision(session, artifact).number}
        out.append(op)
    return out


@router.post("/projects/{key}/changesets", status_code=201, response_model=ChangeSetOut,
             summary="Создать черновик изменений из списка операций")
def create_changeset(key: str, body: ChangeSetIn, request: Request, session: Session = Depends(get_session)):
    domain.get_project(session, key)
    cs = ChangeSet(uid=new_uid(), project_key=key, title=body.title, state="draft",
                   operations=stamp_operations(session, key, body.operations), created_at=utcnow())
    session.add(cs)
    domain.audit(session, key, request.state.actor, "changeset.create", cs.uid, {"title": body.title})
    session.commit()
    return _changeset_out(cs)


@router.put("/projects/{key}/changesets/{uid}", response_model=ChangeSetOut,
            summary="Изменить черновик: название и операции (применённый не меняется)")
def edit_changeset(key: str, uid: str, body: dict, request: Request, session: Session = Depends(get_session)):
    cs = _get_changeset(session, key, uid)
    if cs.state != "draft":
        raise domain.DomainError(409, "CHANGESET_NOT_DRAFT", "Применённый ChangeSet не редактируется")
    if "title" in body:
        cs.title = str(body["title"]).strip()[:300] or cs.title
    if "operations" in body:
        ops = body["operations"]
        if not isinstance(ops, list) or not (1 <= len(ops) <= 1000) or not all(isinstance(o, dict) for o in ops):
            raise domain.DomainError(422, "INVALID_OPERATIONS", "operations: список из 1–1000 объектов")
        cs.operations = stamp_operations(session, key, ops)
    domain.audit(session, key, request.state.actor, "changeset.edit", uid, {"operations": len(cs.operations)})
    session.commit()
    return _changeset_out(cs)


@router.get("/projects/{key}/changesets", response_model=list[ChangeSetOut], summary="Черновики и применённые ChangeSet")
def list_changesets(key: str, response: Response, offset: int = Query(default=0, ge=0),
                    limit: int = Query(default=20, ge=1, le=500), session: Session = Depends(get_session)):
    domain.get_project(session, key)
    rows = session.scalars(select(ChangeSet).where(ChangeSet.project_key == key).order_by(ChangeSet.created_at))
    return _page(response, [_changeset_out(cs) for cs in rows], offset, limit)


@router.get("/projects/{key}/changesets/{uid}", response_model=ChangeSetOut, summary="ChangeSet с операциями")
def get_changeset(key: str, uid: str, session: Session = Depends(get_session)):
    return _changeset_out(_get_changeset(session, key, uid))


@router.get("/projects/{key}/changesets/{uid}/review", summary="ChangeSet для просмотра: операции понятным языком и проверка, пройдёт ли применение")
def review_changeset(key: str, uid: str, session: Session = Depends(get_session)):
    cs = _get_changeset(session, key, uid)
    draft = cs.state == "draft"
    ops = importer.describe_operations(session, key, cs.operations, check=draft)
    out = _changeset_out(cs).model_dump(mode="json")
    first_error = next((o for o in ops if o.get("status") == "error"), None)
    counts: dict[str, int] = {}
    for o in ops:
        counts[o["kind"]] = counts.get(o["kind"], 0) + 1
    source = None
    if cs.source:
        report = cs.source.get("report", {})
        source = {"name": cs.source["name"], "sha256": cs.source["sha256"], "lines": len(cs.source["text"].splitlines()),
                  "via": report.get("via", "import"), "fragments": [importer.normalize_fragment(f) for f in report.get("fragments", [])],
                  "text": cs.source["text"] if draft else None}
    return {"changeset": out, "operations": ops, "counts": counts, "checked": draft,
            "valid": draft and first_error is None, "first_error": first_error, "source": source}


@router.delete("/projects/{key}/changesets/{uid}", summary="Отклонить черновик: он удаляется, применённые удалять нельзя")
def discard_changeset(key: str, uid: str, request: Request, session: Session = Depends(get_session)):
    cs = _get_changeset(session, key, uid)
    if cs.state != "draft":
        raise domain.DomainError(409, "CHANGESET_APPLIED", "Применённый ChangeSet удалять нельзя: он часть истории")
    domain.audit(session, key, request.state.actor, "changeset.discard", uid, {"title": cs.title, "operations": len(cs.operations)})
    session.delete(cs)
    session.commit()
    return {"discarded": uid}


@router.post("/projects/{key}/changesets/{uid}/apply", response_model=ChangeSetOut,
             summary="Применить ChangeSet целиком; при ошибке ничего не записывается")
def apply_changeset(key: str, uid: str, request: Request, session: Session = Depends(get_session)):
    cs = _get_changeset(session, key, uid)
    if cs.state != "draft":
        raise domain.DomainError(409, "CHANGESET_NOT_DRAFT", "ChangeSet уже применён")
    try:
        importer.apply_operations(session, key, cs.operations)
    except domain.DomainError as exc:
        session.rollback()  # ни одна операция не остаётся записанной (NFR-001)
        failed = _get_changeset(session, key, uid)
        failed.last_error = exc.message
        session.commit()
        raise
    cs.state = "applied"
    cs.applied_at = utcnow()
    cs.last_error = None
    domain.audit(session, key, request.state.actor, "changeset.apply", uid,
                 {"title": cs.title, "operations": len(cs.operations)})
    if cs.source:
        importer.record_from_changeset(session, key, cs.source)
    session.commit()
    return _changeset_out(cs)


@router.post("/projects/{key}/imports/preview", summary="Предпросмотр импорта таблиц: элементы, связи и строки, которые не разобрались")
def import_preview(key: str, body: ImportPreviewIn, session: Session = Depends(get_session)):
    return importer.build_preview(session, key, body.text)


@router.post("/projects/{key}/drafts", status_code=201,
             summary="Черновик из отредактированных элементов и связей (из таблиц или от модели) с сохранением источника")
def create_draft(key: str, body: DraftIn, session: Session = Depends(get_session)):
    import hashlib
    domain.get_project(session, key)
    taken: dict[str, int] = {}
    refs: dict[str, str] = {}
    types: dict[str, str] = {}
    operations, fragments, changes, quotes = [], [], [], []
    for item in body.items:
        if item.ref in refs:
            raise domain.DomainError(422, "DUPLICATE_REF", f"Имя {item.ref} повторяется в черновике")
        existing = None
        if item.display_id:
            existing = session.scalar(select(Artifact).where(Artifact.project_key == key, Artifact.display_id == item.display_id))
        attrs = {**(domain.latest_revision(session, existing).attributes if existing else {})}
        if item.priority:
            attrs["priority"] = item.priority
        if item.quote:
            attrs["provenance"] = {"source": "llm", "model": item.model or "", "quote": item.quote, "quote_verified": bool(item.quote_verified)}
        if existing is not None:
            display = existing.display_id
            operations.append({"op": "add_revision", "display_id": display, "title": item.title, "text": item.text, "attributes": attrs})
            changes.append({"display_id": display, "action": "revised"})
        else:
            if item.display_id:
                display, number = item.display_id, item.number or int(item.display_id.rsplit("-", 1)[-1])
            else:
                if item.type not in taken:
                    last = session.scalar(select(Artifact.number).where(Artifact.project_key == key, Artifact.type == item.type)
                                          .order_by(Artifact.number.desc()).limit(1)) or 0
                    taken[item.type] = max(last, *(int(o["number"]) for o in operations if o["op"] == "create_artifact" and o["type"] == item.type), 0) + 1
                number = taken[item.type]
                taken[item.type] += 1
                display = f"{key}-{item.type}-{number:03d}"
            operations.append({"op": "create_artifact", "display_id": display, "type": item.type, "number": number,
                               "title": item.title, "text": item.text, "attributes": attrs,
                               **({"external_key": item.external_key} if item.external_key else {})})
            changes.append({"display_id": display, "action": "created"})
        refs[item.ref] = display
        types[display] = item.type
        if item.line:
            fragments.append({"display_id": display, "external_key": item.external_key, "line": item.line, "end_line": item.line,
                              "via": "table", "cols": item.cols})
        elif item.quote:
            quotes.append({"display_id": display, "quote": item.quote, "verified": bool(item.quote_verified)})

    def resolve(name: str) -> tuple[str, str]:
        if name in refs:
            return refs[name], types[refs[name]]
        art = session.scalar(select(Artifact).where(Artifact.project_key == key, Artifact.display_id == name))
        if art is None:
            raise domain.DomainError(422, "LINK_TARGET_NOT_FOUND", f"Для связи не найден элемент {name}")
        return art.display_id, art.type
    seen = set()
    for rel in body.relations:
        s, st = resolve(rel.source)
        t_, tt = resolve(rel.target)
        allowed_s, allowed_t = domain.RELATION_RULES[rel.type]
        if st not in allowed_s or tt not in allowed_t:
            raise domain.DomainError(422, "INVALID_RELATION", f"Связь {rel.type} недопустима: {s} ({st}) → {t_} ({tt})")
        if s == t_ or (rel.type, s, t_) in seen:
            continue
        seen.add((rel.type, s, t_))
        operations.append({"op": "create_relation", "type": rel.type, "source": s, "target": t_})
    cs = ChangeSet(uid=new_uid(), project_key=key, title=body.title, state="draft", operations=operations, created_at=utcnow())
    if body.source is not None:
        fragments += importer.proposal_fragments(body.source.text, quotes)[0] if quotes else []
        cs.source = {"name": body.source.name, "text": body.source.text,
                     "sha256": hashlib.sha256(body.source.text.encode("utf-8")).hexdigest(),
                     "report": {"fragments": fragments, "created": sum(c["action"] == "created" for c in changes),
                                "revised": sum(c["action"] == "revised" for c in changes), "unchanged": 0,
                                "relations_created": len(seen), "unparsed": [], "changes": changes, "via": "draft"}}
    session.add(cs)
    session.commit()
    return {"changeset": _changeset_out(cs).model_dump(mode="json"), "refs": refs}


@router.get("/projects/{key}/imports/{uid}", response_model=ImportDetail,
            summary="Импорт с исходным текстом (провенанс)")
def get_import(key: str, uid: str, session: Session = Depends(get_session)):
    record = session.scalar(select(ImportSession).where(
        ImportSession.project_key == key, ImportSession.uid == uid))
    if record is None:
        raise domain.DomainError(404, "IMPORT_NOT_FOUND", f"Импорт {uid} не найден")
    return ImportDetail(**importer.report_of(record), source_text=record.source_text)


# --- для скиллов и программного доступа ----------------------------------------

@router.get("/me", summary="Права текущего ключа")
def me(request: Request, session: Session = Depends(get_session)):
    principal = auth.authenticate(session, request.headers.get("authorization"))
    return {"name": principal.name, "kind": principal.kind,
            "project_key": principal.project_key, "role": principal.role or ("admin" if principal.kind == "admin" else None)}


@router.get("/projects/{key}/search", response_model=list[ArtifactOut],
            summary="Поиск по ID, названию и тексту текущих ревизий")
def search(key: str, q: str = Query(min_length=1, max_length=200), type: str | None = Query(default=None),
           limit: int = Query(default=20, ge=1, le=200), session: Session = Depends(get_session)):
    domain.get_project(session, key)
    needle = q.strip().lower()
    query = select(Artifact).where(Artifact.project_key == key).order_by(Artifact.display_id)
    if type:
        query = query.where(Artifact.type == type)
    found = []
    for artifact in session.scalars(query):
        out = _artifact_out(session, artifact)
        haystack = f"{out.display_id} {out.title} {out.text}".lower()
        if needle in haystack:
            found.append(out)
            if len(found) >= limit:
                break
    return found


@router.get("/projects/{key}/context", summary="Контекст для задачи вокруг артефакта (ContextBundle, §10)")
def context(key: str, focus: str, depth: int = Query(default=1, ge=1, le=2), session: Session = Depends(get_session)):
    card = _artifact_out(session, domain.get_artifact(session, key, focus))
    graph = domain.graph(session, key, focus, depth, "both", GRAPH_LIMIT_MAX)
    all_kinds = [i for reason in ("no_reflection", "no_code_link", "unverified_commit")
                 for i in domain.lost_items(session, key, reason=reason)[0]]
    lost_item = next((i for i in all_kinds if i["display_id"] == focus), None)
    drafts = session.scalars(select(ChangeSet).where(ChangeSet.project_key == key, ChangeSet.state == "draft"))
    return {
        "focus": card.model_dump(mode="json"),
        "graph": graph,
        "lost": lost_item,
        "excluded": card.exemption_reason is not None,
        "draft_changesets": [{"uid": d.uid, "title": d.title, "operations": len(d.operations)} for d in drafts],
        "completeness": "truncated" if graph["truncated"] else "complete",
        "generated_at": utcnow().isoformat(),
    }


@router.get("/projects/{key}/impact", summary="Влияние изменения: явные пути и кандидаты (§6.4)")
def impact(key: str, focus: str, depth: int = Query(default=2, ge=1, le=3), session: Session = Depends(get_session)):
    return domain.impact(session, key, focus, depth)


@router.get("/projects/{key}/impact-rules", summary="Правила влияния: встроенные и свои проекта")
def list_impact_rules(key: str, session: Session = Depends(get_session)):
    return domain.list_impact_rules(session, key)


@router.post("/projects/{key}/impact-rules", status_code=201, summary="Создать своё правило влияния")
def create_impact_rule(key: str, body: ImpactRuleIn, request: Request, session: Session = Depends(get_session)):
    out = domain.create_impact_rule(session, key, body.model_dump())
    domain.audit(session, key, request.state.actor, "impact_rule.create", body.code,
                 {"relation": body.relation_type, "direction": body.direction, "neighbors": body.neighbor_types})
    session.commit()
    return out


@router.patch("/projects/{key}/impact-rules/{code}", summary="Включить или выключить своё правило")
def patch_impact_rule(key: str, code: str, body: ImpactRulePatch, request: Request, session: Session = Depends(get_session)):
    out = domain.set_impact_rule_enabled(session, key, code, body.enabled)
    domain.audit(session, key, request.state.actor, "impact_rule.toggle", code, {"enabled": body.enabled})
    session.commit()
    return out


@router.delete("/projects/{key}/impact-rules/{code}", summary="Удалить своё правило влияния")
def delete_impact_rule(key: str, code: str, request: Request, session: Session = Depends(get_session)):
    domain.delete_impact_rule(session, key, code)
    domain.audit(session, key, request.state.actor, "impact_rule.delete", code)
    session.commit()
    return {"deleted": code}


@router.put("/projects/{key}/repo", summary="Подключить репозиторий проекта (read-only, только admin)")
def set_repo(key: str, body: dict, request: Request, session: Session = Depends(get_session)):
    from pth import gitops
    from pth.models import ProjectRepo
    auth.authorize(auth.authenticate(session, request.headers.get("authorization")), None, "writer")
    domain.get_project(session, key)
    try:
        info = gitops.validate_repo(str(body.get("path", "")))
    except gitops.GitError as exc:
        raise domain.DomainError(422, "REPO_INVALID", str(exc))
    repo = session.get(ProjectRepo, key) or ProjectRepo(project_key=key)
    repo.path, repo.head, repo.updated_at = info["path"], info["head"], utcnow()
    session.add(repo)
    domain.audit(session, key, request.state.actor, "repo.connect", info["path"], {"head": info["head"]})
    session.commit()
    return {"path": repo.path, "head": repo.head}


@router.get("/projects/{key}/repo", summary="Состояние подключённого репозитория")
def get_repo(key: str, session: Session = Depends(get_session)):
    from pth import gitops
    from pth.models import ProjectRepo
    domain.get_project(session, key)
    repo = session.get(ProjectRepo, key)
    if repo is None:
        return {"connected": False, "note": "Без репозитория проверки CI не подтверждаются по Git (§13.2)."}
    try:
        now = gitops.validate_repo(repo.path)
        return {"connected": True, "path": repo.path, "head": now["head"], "available": True}
    except gitops.GitError as exc:
        return {"connected": True, "path": repo.path, "available": False, "note": str(exc)}


@router.post("/projects/{key}/ci/runs", status_code=201, summary="Результаты прогона тестов для коммита (от CI или раннера)")
def ci_run(key: str, body: dict, request: Request, session: Session = Depends(get_session)):
    from pth import gitops
    from pth.models import ProjectRepo
    domain.get_project(session, key)
    commit = str(body.get("commit", "")).strip().lower()
    results = body.get("results", [])
    if not isinstance(results, list) or not results or len(results) > 500:
        raise domain.DomainError(422, "INVALID_RESULTS", "results: список 1–500 записей {test, result}")
    repo = session.get(ProjectRepo, key)
    oid = None
    if repo is not None:
        try:
            oid = gitops.resolve_commit(repo.path, commit)
        except gitops.GitError:
            oid = None
        if oid is None:
            raise domain.DomainError(422, "COMMIT_NOT_FOUND", f"Коммит {commit or '—'} не найден в подключённом репозитории")
    if oid is not None:
        # Коммит, на котором прошёл CI, регистрируется сам: ручная регистрация не нужна.
        info = gitops.show_commit(repo.path, oid)
        if info:
            domain.ensure_commit(session, key, oid, info["subject"], info["author"], info["authored_at"])
    trust = "trusted_ci" if request.state.principal_kind == "service" else "human_recorded"
    created = domain.record_ci_results(session, key, commit, oid, results, trust,
                                       request.state.actor, body.get("environment", {}))
    domain.audit(session, key, request.state.actor, "ci.run", commit, {"tests": len(created), "verified": oid is not None})
    session.commit()
    return {"commit": commit, "commit_oid": oid, "verified": oid is not None, "trust_level": trust,
            "evidence": created}


@router.get("/projects/{key}/review", summary="Замечания по правилам качества с состоянием решения")
def review(response: Response, key: str, severity: str | None = Query(default=None, pattern="^(error|warning|info)$"),
           offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=500),
           session: Session = Depends(get_session)):
    findings = domain.review_state(session, key, domain.review(session, key))
    if severity:
        findings = [f for f in findings if f["severity"] == severity]
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warning", "info")}
    total = len(findings)
    # Пояснения всех правил проекта, а не только сработавших: справка видна и до первого замечания.
    return {"counts": counts, "total": total, "findings": findings[offset:offset + limit],
            "rules": domain.REVIEW_RULE_TEXT}


@router.post("/projects/{key}/review/decisions", status_code=201, summary="Принять или снять замечание с причиной")
def decide(key: str, body: dict, request: Request, session: Session = Depends(get_session)):
    d = domain.decide_review(session, key, str(body.get("rule", "")), str(body.get("display_id", "")),
                             str(body.get("state", "")), str(body.get("reason", "")), request.state.actor)
    session.commit()
    return {"rule": d.rule, "display_id": d.display_id, "state": d.state, "revision": d.revision, "reason": d.reason}


@router.get("/projects/{key}/audit", summary="Журнал аудита проекта (новые сверху)")
def audit_log(response: Response, key: str, offset: int = Query(default=0, ge=0),
              limit: int = Query(default=20, ge=1, le=500), session: Session = Depends(get_session)):
    from pth.models import AuditEvent
    domain.get_project(session, key)
    total = session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.project_key == key)) or 0
    response.headers["X-Total-Count"] = str(total)
    rows = session.scalars(select(AuditEvent).where(AuditEvent.project_key == key)
                           .order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit)).all()
    return [{"actor": e.actor, "action": e.action, "target": e.target, "details": e.details,
             "created_at": e.created_at.isoformat()} for e in rows]


@router.patch("/projects/{key}", response_model=ProjectOut, summary="Переименовать проект (только admin)")
def rename_project(key: str, body: dict, request: Request, session: Session = Depends(get_session)):
    auth.authorize(auth.authenticate(session, request.headers.get("authorization")), None, "writer")  # только admin
    project = domain.get_project(session, key)
    name = str(body.get("name", "")).strip()
    if not (1 <= len(name) <= 200):
        raise domain.DomainError(422, "INVALID_NAME", "Название от 1 до 200 символов")
    old = project.name
    project.name = name
    domain.audit(session, key, request.state.actor, "project.rename", key, {"from": old, "to": name})
    session.commit()
    return ProjectOut(key=project.key, name=project.name, created_at=project.created_at)


@router.get("/projects/{key}/baselines/compare", summary="Сравнение двух снимков")
def compare(key: str, a: str, b: str, session: Session = Depends(get_session)):
    return domain.compare_baselines(session, key, a, b)


@router.get("/projects/{key}/commits/{sha}", summary="Карточка коммита")
def commit_detail(key: str, sha: str, session: Session = Depends(get_session)):
    for c in list_commits(key, session=session):
        if c["sha"] == sha or (c["sha"] or "").startswith(sha):
            return {**c, "evidence": [e for e in list_evidence(key, session=session) if e["commit_sha"] == c["sha"]],
                    "reachability": "не проверено: нет подключения к Git-репозиторию"}
    raise domain.DomainError(404, "COMMIT_NOT_FOUND", f"Коммит {sha} не найден")


def _repo_or_409(session: Session, key: str):
    from pth.models import ProjectRepo
    repo = session.get(ProjectRepo, key)
    if repo is None:
        raise domain.DomainError(409, "REPO_NOT_CONNECTED", "Репозиторий проекта не подключён: коммиты читаются из Git")
    return repo


def _mentions(a, key: str, text: str) -> list[str]:
    import re
    ids = sorted(set(re.findall(rf"{re.escape(key)}-(?:US|FR|NFR|AC|ADR|TEST)-\d+", text)))
    return [i for i in ids if any(x.display_id == i for x in a.by_uid.values())]


@router.get("/projects/{key}/git/commits", summary="Коммиты из подключённого репозитория с данными PTH: связи, проверки, упоминания")
def git_commits(response: Response, key: str, offset: int = Query(default=0, ge=0),
                limit: int = Query(default=20, ge=1, le=100), session: Session = Depends(get_session)):
    """Список читается прямо из Git, регистрировать коммиты не нужно. Связь с требованиями и проверки подтягиваются из PTH."""
    from pth import gitops
    from pth.models import Evidence
    domain.get_project(session, key)
    repo = _repo_or_409(session, key)
    try:
        page = gitops.log_page(repo.path, offset, limit)
    except gitops.GitError as exc:
        raise domain.DomainError(422, "REPO_UNAVAILABLE", str(exc))
    a = domain.analyze(session, key)
    registered = {x.external_key: x for x in a.by_uid.values() if x.type == "COMMIT" and x.external_key}
    checks: dict[str, dict] = {}
    for e in domain.latest_per_commit(session.scalars(select(Evidence).where(Evidence.project_key == key)).all()):
        c = checks.setdefault(e.commit_oid or e.commit_sha or "", {"total": 0, "passed": 0, "failed": 0})
        c["total"] += 1
        c["passed"] += e.result == "pass"
        c["failed"] += e.result == "fail"
    items = []
    for row in page["items"]:
        reg = registered.get(row["sha"])
        implements = sorted(a.by_uid[r.target_uid].display_id for r in a.relations
                            if reg and r.type == "implements" and r.source_uid == reg.uid)
        items.append({**row, "registered": reg.display_id if reg else None, "implements": implements,
                      "mentions": _mentions(a, key, row["title"]), "checks": checks.get(row["sha"])})
    response.headers["X-Total-Count"] = str(page["total"])
    return items


@router.post("/projects/{key}/git/sync", summary="Зарегистрировать в PTH все коммиты репозитория, которых ещё нет (идемпотентно)")
def git_sync(key: str, request: Request, session: Session = Depends(get_session)):
    from pth import gitops
    domain.get_project(session, key)
    repo = _repo_or_409(session, key)
    try:
        out = domain.sync_commits(session, key, repo.path)
    except gitops.GitError as exc:
        raise domain.DomainError(422, "REPO_UNAVAILABLE", str(exc))
    if out["registered"]:
        domain.audit(session, key, request.state.actor, "commit.autoregister", None, out)
    session.commit()
    return out


@router.post("/projects/{key}/commits/{sha}/implements", summary="Добавить связи implements к уже зарегистрированному коммиту")
def link_commit(key: str, sha: str, body: CommitLinkIn, request: Request, session: Session = Depends(get_session)):
    commit, added = domain.link_commit(session, key, sha, body.implements)
    domain.audit(session, key, request.state.actor, "commit.link", sha, {"implements": added})
    session.commit()
    return {"sha": sha, "display_id": commit.display_id, "added": added}


# --- релизы ------------------------------------------------------------------------

@router.get("/projects/{key}/releases", summary="Релизы проекта с продвижением")
def list_releases(key: str, session: Session = Depends(get_session)):
    return releases.list_releases(session, key)


@router.post("/projects/{key}/releases", status_code=201, summary="Создать релиз (набор состава планируется отдельно)")
def create_release(key: str, body: ReleaseIn, request: Request, session: Session = Depends(get_session)):
    rel = releases.create_release(session, key, body.name, body.description, request.state.actor)
    domain.audit(session, key, request.state.actor, "release.create", rel.name)
    session.commit()
    return releases.release_out(session, key, rel)


@router.post("/projects/{key}/releases/move", summary="Перенести элемент между релизами (null — «не запланировано»)")
def move_release_item(key: str, body: ReleaseMoveIn, request: Request, session: Session = Depends(get_session)):
    out = releases.move_item(session, key, body.display_id, body.from_release, body.to_release, body.with_related)
    domain.audit(session, key, request.state.actor, "release.move", body.display_id,
                 {"from": body.from_release, "to": body.to_release, **out})
    session.commit()
    return out


@router.get("/projects/{key}/releases/compare", summary="Сравнить состав двух релизов")
def compare_releases(key: str, a: str, b: str, session: Session = Depends(get_session)):
    return releases.compare(session, key, a, b)


@router.get("/projects/{key}/releases/{uid}/report", summary="Отчёт по релизу в Markdown")
def release_report(key: str, uid: str, session: Session = Depends(get_session)):
    return Response(releases.report_markdown(session, key, releases._release(session, key, uid)), media_type="text/markdown; charset=utf-8")


@router.get("/projects/{key}/releases/{uid}", summary="Релиз: состав, продвижение, решение")
def get_release(key: str, uid: str, session: Session = Depends(get_session)):
    return releases.release_out(session, key, releases._release(session, key, uid))


@router.post("/projects/{key}/releases/{uid}/items", summary="Добавить в релиз US, ФТ, НФТ, AC; with_related берёт уточняющие требования")
def add_release_items(key: str, uid: str, body: ReleaseItemsIn, request: Request, session: Session = Depends(get_session)):
    rel = releases._release(session, key, uid)
    added = releases.add_items(session, key, rel, body.display_ids, body.with_related)
    domain.audit(session, key, request.state.actor, "release.add", rel.name, {"added": [x["display_id"] for x in added]})
    session.commit()
    return {"added": added}


@router.delete("/projects/{key}/releases/{uid}/items/{display_id}", summary="Убрать элемент из релиза")
def remove_release_item(key: str, uid: str, display_id: str, request: Request, with_related: bool = False,
                        session: Session = Depends(get_session)):
    rel = releases._release(session, key, uid)
    removed = releases.remove_item(session, key, rel, display_id, with_related)
    domain.audit(session, key, request.state.actor, "release.remove", rel.name, {"removed": removed})
    session.commit()
    return {"removed": removed}


@router.post("/projects/{key}/releases/{uid}/freeze", summary="Зафиксировать состав: запоминаются ревизии элементов")
def freeze_release(key: str, uid: str, request: Request, session: Session = Depends(get_session)):
    rel = releases._release(session, key, uid)
    releases.freeze(session, key, rel)
    domain.audit(session, key, request.state.actor, "release.freeze", rel.name)
    session.commit()
    return releases.release_out(session, key, rel)


@router.post("/projects/{key}/releases/{uid}/unfreeze", summary="Снять фиксацию (с причиной)")
def unfreeze_release(key: str, uid: str, body: ReleaseReasonIn, request: Request, session: Session = Depends(get_session)):
    rel = releases._release(session, key, uid)
    releases.unfreeze(session, key, rel, body.reason)
    domain.audit(session, key, request.state.actor, "release.unfreeze", rel.name, {"reason": body.reason})
    session.commit()
    return releases.release_out(session, key, rel)


@router.post("/projects/{key}/releases/{uid}/release", summary="Выпустить релиз по тегу репозитория; с незакрытыми элементами нужна причина")
def do_release(key: str, uid: str, body: ReleaseDoIn, request: Request, session: Session = Depends(get_session)):
    rel = releases._release(session, key, uid)
    snap = releases.do_release(session, key, rel, body.tag, body.reason, request.state.actor)
    domain.audit(session, key, request.state.actor, "release.release", rel.name,
                 {"tag": body.tag, "commit": rel.commit_oid, "blockers": len(snap["blockers"]), "reason": body.reason})
    session.commit()
    return releases.release_out(session, key, rel)


@router.post("/projects/{key}/releases/{uid}/cancel", summary="Отменить релиз (с причиной)")
def cancel_release(key: str, uid: str, body: ReleaseReasonIn, request: Request, session: Session = Depends(get_session)):
    rel = releases._release(session, key, uid)
    releases.cancel(session, key, rel, body.reason, request.state.actor)
    domain.audit(session, key, request.state.actor, "release.cancel", rel.name, {"reason": body.reason})
    session.commit()
    return releases.release_out(session, key, rel)


@router.get("/projects/{key}/story-map", summary="USM (карта историй): активности × релизы, под US — ФТ")
def story_map(key: str, session: Session = Depends(get_session)):
    return releases.story_map(session, key)


@router.get("/projects/{key}/git/info", summary="Сведения о репозитории: ветка, авторы, теги, структура")
def git_info(key: str, session: Session = Depends(get_session)):
    from pth import gitops
    domain.get_project(session, key)
    repo = _repo_or_409(session, key)
    try:
        return {"path": repo.path, **gitops.repo_info(repo.path)}
    except gitops.GitError as exc:
        raise domain.DomainError(422, "REPO_UNAVAILABLE", str(exc))


@router.get("/projects/{key}/git/commits/{sha}/diff", summary="Diff одного файла коммита по блокам и строкам")
def git_commit_diff(key: str, sha: str, path: str, session: Session = Depends(get_session)):
    from pth import gitops
    domain.get_project(session, key)
    repo = _repo_or_409(session, key)
    oid = gitops.resolve_commit(repo.path, sha)
    diff = gitops.file_diff(repo.path, oid, path) if oid else None
    if diff is None:
        raise domain.DomainError(404, "DIFF_NOT_FOUND", f"Не удалось получить diff {path} в коммите {sha}")
    return diff


@router.get("/projects/{key}/git/commits/{sha}", summary="Карточка коммита: Git (автор, сообщение, файлы), требования, проверки")
def git_commit_detail(key: str, sha: str, session: Session = Depends(get_session)):
    from pth import gitops
    domain.get_project(session, key)
    repo = _repo_or_409(session, key)
    oid = gitops.resolve_commit(repo.path, sha)
    info = gitops.show_commit(repo.path, oid) if oid else None
    if info is None:
        raise domain.DomainError(404, "COMMIT_NOT_FOUND", f"Коммит {sha} не найден в репозитории")
    a = domain.analyze(session, key)
    title = lambda d: a.revisions[next(u for u, x in a.by_uid.items() if x.display_id == d)].title  # noqa: E731
    try:
        runs = commit_runs(key, oid, session=session)
    except domain.DomainError:
        runs = {"registered": None, "implements": [], "runs": []}
    mention_ids = _mentions(a, key, f"{info['subject']}\n{info['body']}")
    head = domain.current_head(session, key)
    return {**info, "is_head": head == oid, "registered": runs["registered"],
            "implements": [{"id": i, "title": title(i)} for i in runs["implements"]],
            "mentions": [{"id": i, "title": title(i)} for i in mention_ids if i not in runs["implements"]],
            "runs": runs["runs"]}


@router.get("/projects/{key}/runs", summary="Наборы запусков: все тесты, прогнанные на одном коммите, одной строкой")
def run_sets(response: Response, key: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=200),
             session: Session = Depends(get_session)):
    """Набор — это проверка коммита: тесты и проверки CI, выполненные на нём. По каждому тесту берётся последний запуск."""
    from pth.models import Evidence, EvidenceAttachment
    from sqlalchemy import func
    domain.get_project(session, key)
    a = domain.analyze(session, key)
    rows = domain.latest_per_commit(session.scalars(
        select(Evidence).where(Evidence.project_key == key).order_by(Evidence.created_at.desc())).all())
    attached = dict(session.execute(select(EvidenceAttachment.evidence_uid, func.count()).where(
        EvidenceAttachment.project_key == key).group_by(EvidenceAttachment.evidence_uid)).all())
    sets: dict[str, dict] = {}
    for e in rows:
        commit = e.commit_oid or e.commit_sha or ""
        group = sets.setdefault(commit, {"commit": commit or None, "ran_at": e.created_at.isoformat(), "results": []})
        test = a.by_uid.get(e.test_uid)
        kind = a.revisions[e.test_uid].attributes.get("kind") if test else None
        group["results"].append({"test": test.display_id if test else None,
                                 "title": a.revisions[e.test_uid].title if test else None,
                                 "kind": kind if kind in domain.CHECK_KINDS else "unit", "result": e.result,
                                 "ran_at": e.created_at.isoformat(), "trust": e.trust_level, "verified": e.verified,
                                 "issuer": e.issuer, "attachments": attached.get(e.uid, 0)})
    out = []
    for group in sets.values():
        res = group["results"]
        group.update(total=len(res), passed=sum(r["result"] == "pass" for r in res), failed=sum(r["result"] == "fail" for r in res),
                     other=sum(r["result"] not in ("pass", "fail") for r in res),
                     kinds=sorted({r["kind"] for r in res}, key=list(domain.CHECK_KINDS).index),
                     attachments=sum(r["attachments"] for r in res),
                     sources=sorted({r["trust"] for r in res}))
        out.append(group)
    return _page(response, out, offset, limit)


@router.get("/projects/{key}/commits/{sha}/runs",
            summary="Что произошло на коммите: запуски тестов (хук или вручную) и реализуемые требования")
def commit_runs(key: str, sha: str, session: Session = Depends(get_session)):
    """Работает и для коммитов без карточки COMMIT: хук пишет свидетельство по SHA, даже если коммит не зарегистрирован."""
    from pth.models import Evidence, EvidenceAttachment
    domain.get_project(session, key)
    if len(sha) < 7:
        raise domain.DomainError(422, "SHA_TOO_SHORT", "Нужно минимум 7 символов SHA")
    a = domain.analyze(session, key)
    names = {u: x for u, x in a.by_uid.items()}
    rows = session.scalars(select(Evidence).where(Evidence.project_key == key).order_by(Evidence.created_at.desc())).all()
    runs = []
    full = None
    for e in domain.latest_per_commit(rows):
        c = e.commit_oid or e.commit_sha or ""
        if c and c.startswith(sha):
            full = full or c
            test = names.get(e.test_uid)
            files = [{"uid": x.uid, "name": x.name, "size": x.size, "content_type": x.content_type}
                     for x in session.scalars(select(EvidenceAttachment).where(EvidenceAttachment.evidence_uid == e.uid)
                                              .order_by(EvidenceAttachment.created_at))]
            runs.append({"files": files, "test": test.display_id if test else None,
                         "title": a.revisions[e.test_uid].title if test else None, "result": e.result,
                         "ran_at": e.created_at.isoformat(), "trust": e.trust_level, "verified": e.verified,
                         "issuer": e.issuer})
    registered = next((x for x in a.by_uid.values() if x.type == "COMMIT" and (x.external_key or "").startswith(sha)), None)
    git_title = None
    if registered is None and not runs:
        from pth import gitops
        from pth.models import ProjectRepo
        repo = session.get(ProjectRepo, key)
        oid = gitops.resolve_commit(repo.path, sha) if repo else None
        if oid:
            full = oid
            git_title = gitops._run(repo.path, "log", "-1", "--format=%s", oid).stdout.strip()
    if registered is None and not runs and full is None:
        raise domain.DomainError(404, "COMMIT_NOT_FOUND", f"Коммит {sha} не найден ни среди зарегистрированных, ни среди запусков")
    implements = sorted(a.by_uid[r.target_uid].display_id for r in a.relations
                        if registered and r.type == "implements" and r.source_uid == registered.uid)
    return {"sha": (registered.external_key if registered else full), "registered": registered.display_id if registered else None,
            "title": a.revisions[registered.uid].title if registered else git_title,
            "implements": implements, "runs": runs}


@router.get("/projects/{key}/artifacts/{display_id}/origins", summary="Первоисточник: из каких строк каких документов получено требование")
def artifact_origins(key: str, display_id: str, session: Session = Depends(get_session)):
    """Читает привязку строк, сохранённую при импорте или при разборе текста моделью. Созданный вручную элемент: пустой список."""
    domain.get_artifact(session, key, display_id)
    out = []
    sessions = session.scalars(select(ImportSession).where(ImportSession.project_key == key)
                               .order_by(ImportSession.created_at.desc()))
    for rec in sessions:
        lines = rec.source_text.splitlines()
        for raw in importer.fragments_for(session, key, rec):
            frag = importer.normalize_fragment(raw)
            if frag.get("display_id") == display_id and 1 <= frag["line"] <= len(lines):
                shown = frag.get("quote") or "\n".join(l.strip() for l in lines[frag["line"] - 1:frag["end_line"]])
                out.append({"import_uid": rec.uid, "source_name": rec.source_name, "imported_at": rec.created_at.isoformat(),
                            "line": frag["line"], "end_line": frag["end_line"], "via": frag["via"],
                            "verified": frag.get("verified"), "text": shown[:600]})
    return out


@router.get("/projects/{key}/sources/compare", summary="Сравнить две версии источника построчно и показать затронутые требования")
def source_compare(key: str, a: str, b: str, session: Session = Depends(get_session)):
    import difflib
    recs = {}
    for uid in (a, b):
        rec = session.scalar(select(ImportSession).where(ImportSession.project_key == key, ImportSession.uid == uid))
        if rec is None:
            raise domain.DomainError(404, "IMPORT_NOT_FOUND", f"Импорт {uid} не найден")
        recs[uid] = rec
    ra, rb = recs[a], recs[b]

    def ids_by_line(rec) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for raw in importer.fragments_for(session, key, rec):
            f = importer.normalize_fragment(raw)
            for n in range(f["line"], f["end_line"] + 1):
                out.setdefault(n, []).append(f["display_id"])
        return out
    ids_a, ids_b = ids_by_line(ra), ids_by_line(rb)
    la, lb = ra.source_text.splitlines(), rb.source_text.splitlines()
    matcher = difflib.SequenceMatcher(a=la, b=lb, autojunk=False)
    hunks, removed_ids, added_ids = [], set(), set()
    added = removed = 0
    for group in matcher.get_grouped_opcodes(3):
        lines = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [{"type": "ctx", "old": i + 1, "new": j + 1, "text": la[i], "ids": ids_b.get(j + 1, [])}
                          for i, j in zip(range(i1, i2), range(j1, j2))]
                continue
            for i in range(i1, i2):
                removed += 1
                removed_ids.update(ids_a.get(i + 1, []))
                lines.append({"type": "del", "old": i + 1, "new": None, "text": la[i], "ids": ids_a.get(i + 1, [])})
            for j in range(j1, j2):
                added += 1
                added_ids.update(ids_b.get(j + 1, []))
                lines.append({"type": "add", "old": None, "new": j + 1, "text": lb[j], "ids": ids_b.get(j + 1, [])})
        first, last = group[0], group[-1]
        hunks.append({"header": f"@@ -{first[1] + 1},{last[2] - first[1]} +{first[3] + 1},{last[4] - first[3]} @@", "lines": lines})
    meta = lambda r: {"uid": r.uid, "source_name": r.source_name, "created_at": r.created_at.isoformat()}  # noqa: E731
    return {"a": meta(ra), "b": meta(rb), "identical": ra.source_sha256 == rb.source_sha256, "hunks": hunks,
            "added_lines": added, "removed_lines": removed,
            "requirements": {"changed": sorted(removed_ids & added_ids), "removed": sorted(removed_ids - added_ids),
                             "added": sorted(added_ids - removed_ids)}}


@router.get("/projects/{key}/sources/{uid}", summary="Исходный текст импорта с привязкой формулировок к требованиям")
def source_view(key: str, uid: str, session: Session = Depends(get_session)):
    record = session.scalar(select(ImportSession).where(ImportSession.project_key == key, ImportSession.uid == uid))
    if record is None:
        raise domain.DomainError(404, "IMPORT_NOT_FOUND", f"Импорт {uid} не найден")
    fragments = [importer.normalize_fragment(f) for f in importer.fragments_for(session, key, record)]
    lines = record.source_text.splitlines()
    by_line: dict[int, str] = {}
    for f in fragments:
        for n in range(f["line"], f["end_line"] + 1):
            by_line.setdefault(n, f["display_id"])
    return {"source_name": record.source_name, "source_sha256": record.source_sha256, "created_at": record.created_at.isoformat(),
            "lines": [{"no": i + 1, "text": text, "display_id": by_line.get(i + 1)} for i, text in enumerate(lines)],
            "fragments": len(fragments),
            "marks": [{k: f.get(k) for k in ("display_id", "line", "end_line", "via", "cols", "quote", "verified", "start_col", "end_col")}
                      for f in fragments]}


@router.post("/projects/{key}/evidence", status_code=201, summary="Записать результат проверки (неизменяемо)")
def add_evidence(key: str, body: dict, request: Request, session: Session = Depends(get_session)):
    ev = domain.record_evidence(session, key, body.get("test"), body.get("result"),
                                body.get("commit_sha"), body.get("environment", {}))
    domain.audit(session, key, request.state.actor, "evidence.record", body.get("test"),
                 {"result": ev.result, "commit_sha": ev.commit_sha})
    session.commit()
    return {"uid": ev.uid, "test": body.get("test"), "result": ev.result, "commit_sha": ev.commit_sha,
            "environment": ev.environment, "created_at": ev.created_at.isoformat()}


@router.get("/projects/{key}/tests", summary="Состояние тестов: запускались ли, на каком коммите, для каких требований нет тестов")
def tests_status(key: str, session: Session = Depends(get_session)):
    return domain.test_status(session, key)


@router.get("/projects/{key}/compliance", summary="Соответствие требований проверкам (SK-11)")
def compliance(key: str, session: Session = Depends(get_session)):
    return domain.compliance(session, key)


@router.get("/projects/{key}/readiness", summary="Готовность: ready / not_ready / insufficient_data (SK-13)")
def readiness(key: str, session: Session = Depends(get_session)):
    return domain.readiness(session, key)


@router.get("/projects/{key}/psi", summary="Черновик ПСИ в markdown (SK-10)", response_class=PlainTextResponse)
def psi(key: str, session: Session = Depends(get_session)):
    return domain.psi_markdown(session, key)


@router.post("/projects/{key}/baselines", status_code=201, summary="Зафиксировать базовый снимок (SK-13)")
def create_baseline(key: str, body: dict, request: Request, session: Session = Depends(get_session)):
    base = domain.create_baseline(session, key, str(body.get("name", "baseline"))[:200])
    domain.audit(session, key, request.state.actor, "baseline.create", base.uid, {"name": base.name})
    session.commit()
    return {"uid": base.uid, "name": base.name, "artifacts": len(base.snapshot["artifacts"]),
            "relations": len(base.snapshot["relations"]), "taken_at": base.snapshot["taken_at"]}


@router.get("/projects/{key}/baselines", summary="Базовые снимки проекта")
def list_baselines(key: str, session: Session = Depends(get_session)):
    from pth.models import Baseline
    domain.get_project(session, key)
    rows = session.scalars(select(Baseline).where(Baseline.project_key == key).order_by(Baseline.created_at))
    return [{"uid": b.uid, "name": b.name, "created_at": b.created_at.isoformat(),
             "artifacts": len(b.snapshot["artifacts"])} for b in rows]


@router.get("/projects/{key}/commits", summary="Зарегистрированные коммиты и связанные требования")
def list_commits(key: str, response: Response = None, offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=20, ge=1, le=500), session: Session = Depends(get_session)):
    domain.get_project(session, key)
    a = domain.analyze(session, key)
    out = []
    for art in a.by_uid.values():
        if art.type != "COMMIT":
            continue
        implements = [a.by_uid[r.target_uid].display_id for r in a.relations
                      if r.type == "implements" and r.source_uid == art.uid]
        out.append({"display_id": art.display_id, "sha": art.external_key,
                    "title": a.revisions[art.uid].title, "implements": implements,
                    "created_at": art.created_at.isoformat()})
    out = sorted(out, key=lambda c: c["created_at"], reverse=True)
    if response is not None:
        return _page(response, out, offset, limit)
    return out


@router.post("/projects/{key}/evidence/{uid}/attachments", status_code=201,
             summary="Приложить файл к запуску (лог, отчёт нагрузочного теста, Allure-архив): тело запроса — содержимое файла")
async def add_attachment(key: str, uid: str, request: Request, name: str, session: Session = Depends(get_session)):
    data = await request.body()
    out = domain.add_attachment(session, key, uid, name, request.headers.get("content-type", ""), data)
    domain.audit(session, key, request.state.actor, "evidence.attach", uid, {"name": out["name"], "size": out["size"]})
    session.commit()
    return out


@router.get("/projects/{key}/attachments", summary="Файлы запусков: список с хешами, без содержимого")
def list_attachments(key: str, evidence: str | None = None, session: Session = Depends(get_session)):
    from pth.models import EvidenceAttachment
    domain.get_project(session, key)
    q = select(EvidenceAttachment).where(EvidenceAttachment.project_key == key).order_by(EvidenceAttachment.created_at.desc())
    if evidence:
        q = q.where(EvidenceAttachment.evidence_uid == evidence)
    return [domain.attachment_out(a) for a in session.scalars(q)]


PREVIEW_LIMIT = 200 * 1024


@router.get("/projects/{key}/attachments/{uid}/preview", summary="Просмотр файла запуска: текст и сводка, без скачивания")
def preview_attachment(key: str, uid: str, session: Session = Depends(get_session)):
    from pth.models import EvidenceAttachment
    att = session.get(EvidenceAttachment, uid)
    if att is None or att.project_key != key:
        raise domain.DomainError(404, "ATTACHMENT_NOT_FOUND", f"Файл {uid} не найден")
    name = att.name.lower()
    summary = None
    if name.endswith(".zip"):
        s = domain.allure_summary(att.data)
        summary = {"type": "allure", **s} if s else None
    else:
        s = domain.sarif_summary(att.data)
        if s:
            summary = {"type": "sarif", **s, "top_rules": [list(x) for x in s["top_rules"]]}
        else:
            g = domain.quality_gate_summary(att.data)
            summary = {"type": "quality_gate", **g} if g else None
    text, truncated = None, False
    if not name.endswith((".zip", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".gz", ".tar", ".bin")):
        raw = att.data[:PREVIEW_LIMIT]
        if b"\x00" not in raw:
            text = raw.decode("utf-8", errors="replace")
            truncated = len(att.data) > PREVIEW_LIMIT
            if name.endswith((".json", ".sarif")):
                try:
                    import json
                    if not truncated:
                        text = json.dumps(json.loads(att.data), ensure_ascii=False, indent=2)
                except ValueError:
                    pass
    return {**domain.attachment_out(att), "summary": summary, "text": text, "truncated": truncated}


@router.get("/projects/{key}/attachments/{uid}", summary="Скачать файл запуска")
def download_attachment(key: str, uid: str, session: Session = Depends(get_session)):
    from pth.models import EvidenceAttachment
    att = session.get(EvidenceAttachment, uid)
    if att is None or att.project_key != key:
        raise domain.DomainError(404, "ATTACHMENT_NOT_FOUND", f"Файл {uid} не найден")
    # attachment + nosniff: загруженный HTML не исполняется в origin приложения
    return Response(content=att.data, media_type=att.content_type, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(att.name)}", "X-Content-Type-Options": "nosniff"})


@router.get("/projects/{key}/evidence", summary="Записанные свидетельства проверок")
def list_evidence(key: str, response: Response = None, offset: int = Query(default=0, ge=0),
                  limit: int = Query(default=20, ge=1, le=500), all_runs: bool = False,
                  session: Session = Depends(get_session)):
    """По умолчанию без повторов на одном коммите; all_runs=true — полный журнал."""
    from pth.models import Evidence
    domain.get_project(session, key)
    rows = session.scalars(select(Evidence).where(Evidence.project_key == key).order_by(Evidence.created_at.desc())).all()
    if not all_runs:
        rows = domain.latest_per_commit(rows)
    tests = {a.uid: a.display_id for a in session.scalars(select(Artifact).where(Artifact.project_key == key))}
    out = [{"uid": e.uid, "test": tests.get(e.test_uid), "result": e.result, "commit_sha": e.commit_sha,
            "commit_oid": e.commit_oid, "verified": e.verified, "trust_level": e.trust_level, "issuer": e.issuer,
            "environment": e.environment, "created_at": e.created_at.isoformat()} for e in rows]
    if response is not None:
        return _page(response, out, offset, limit)
    return out


@router.get("/projects/{key}/baselines/{uid}", summary="Снимок: состав ревизий и связей")
def get_baseline(key: str, uid: str, session: Session = Depends(get_session)):
    from pth.models import Baseline
    base = session.scalar(select(Baseline).where(Baseline.project_key == key, Baseline.uid == uid))
    if base is None:
        raise domain.DomainError(404, "BASELINE_NOT_FOUND", f"Снимок {uid} не найден")
    return {"uid": base.uid, "name": base.name, "created_at": base.created_at.isoformat(), **base.snapshot}


@router.get("/keys", summary="API-ключи (без токенов; только admin)")
def list_keys(request: Request, session: Session = Depends(get_session)):
    from pth.models import ApiKey
    rows = session.scalars(select(ApiKey).order_by(ApiKey.created_at)).all()
    return [{"uid": k.uid, "name": k.name, "kind": k.kind, "project_key": k.project_key, "role": k.role,
             "revoked": k.revoked_at is not None, "created_at": k.created_at.isoformat()} for k in rows]


@router.post("/keys", status_code=201, summary="Выпустить ключ; токен возвращается один раз (только admin)")
def create_key(body: dict, request: Request, session: Session = Depends(get_session)):
    kind = body.get("kind", "service")
    try:
        token = auth.issue_key(session, name=str(body.get("name", "key"))[:200], kind=kind,
                               project_key=body.get("project_key"), role=body.get("role"))
    except ValueError as exc:
        raise domain.DomainError(422, "INVALID_KEY", str(exc))
    if body.get("project_key"):
        domain.audit(session, body["project_key"], request.state.actor, "key.issue", str(body.get("name")),
                     {"role": body.get("role")})
    session.commit()
    return {"token": token, "kind": kind, "project_key": body.get("project_key"), "role": body.get("role")}


@router.post("/keys/{uid}/revoke", summary="Отозвать ключ (только admin)")
def revoke_key(uid: str, session: Session = Depends(get_session)):
    auth.revoke(session, uid)
    session.commit()
    return {"uid": uid, "revoked": True}


@router.get("/projects/{key}/status", summary="Сводка проекта: счётчики по типам и состоянию")
def status(key: str, session: Session = Depends(get_session)):
    domain.get_project(session, key)
    by_type: dict[str, int] = {}
    for artifact in session.scalars(select(Artifact).where(Artifact.project_key == key)):
        by_type[artifact.type] = by_type.get(artifact.type, 0) + 1
    lost_total, excluded = domain.lost_items(session, key)
    lost_total = len(lost_total)
    drafts = session.scalar(select(func.count()).select_from(ChangeSet).where(
        ChangeSet.project_key == key, ChangeSet.state == "draft")) or 0
    imports = session.scalar(select(func.count()).select_from(ImportSession).where(ImportSession.project_key == key)) or 0
    return {"artifacts": by_type, "lost": lost_total, "excluded": len(excluded),
            "draft_changesets": drafts, "imports": imports,
            "relations": session.scalar(select(func.count()).select_from(Relation).where(Relation.project_key == key)) or 0}

# --- анализ --------------------------------------------------------------------

@router.get("/projects/{key}/lost", response_model=LostOut,
            summary="Потерянные элементы (reason=no_reflection по умолчанию), не реализованные (no_code_link) или реализованные непроверенным коммитом (unverified_commit)")
def lost(
    response: Response,
    key: str,
    type: str | None = Query(default=None),
    priority: str | None = Query(default=None, pattern=r"^P[0-3]$"),
    reason: str = Query(default="no_reflection", pattern="^(no_code_link|no_reflection|unverified_commit)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=500),
    session: Session = Depends(get_session),
):
    items, excluded = domain.lost_items(session, key, type, priority, reason)
    total = len(items)
    return LostOut(total=total, items=items[offset:offset + limit], excluded=excluded)


@router.get("/projects/{key}/graph", response_model=GraphOut,
            summary="Локальный подграф вокруг артефакта")
def graph(
    key: str,
    focus: str,
    depth: int = Query(default=2, ge=1, le=6),
    direction: Direction = "both",
    limit: int = Query(default=GRAPH_LIMIT_MAX, ge=1, le=GRAPH_LIMIT_MAX),
    relation_types: str | None = Query(default=None, description="Через запятую: refines,motivates,implements"),
    session: Session = Depends(get_session),
):
    types = set(relation_types.split(",")) if relation_types else None
    if types and not types <= {"refines", "motivates", "implements"}:
        raise domain.DomainError(422, "INVALID_FILTER", "Неизвестный тип связи в relation_types")
    return domain.graph(session, key, focus, depth, direction, limit, types)


def _mark_interrupted_jobs(session_factory) -> None:
    """Фоновые задачи живут в памяти процесса: после перезапуска их не продолжить, только зафиксировать."""
    with session_factory() as session:
        stale = session.scalars(select(ExtractionJob).where(ExtractionJob.state.in_(["queued", "running"]))).all()
        for job in stale:
            job.state = "failed"
            job.finished_at = utcnow()
            job.errors = [*job.errors, "задача прервана перезапуском сервера; запустите выделение заново"]
        if stale:
            session.commit()


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or os.environ.get("PTH_DATABASE_URL", DEFAULT_DB)
    engine = make_engine(url)
    from pth.migrations import migrate
    migrate(engine)

    app = FastAPI(
        title="Project Traceability Hub API",
        version="0.1.0",
        description="Минимальный срез API по спеке PTH v0.1: проекты, артефакты с ревизиями, связи, коммиты, потерянные элементы, граф. Аутентификация и ChangeSet ещё не реализованы.",
    )
    app.state.session_factory = make_session_factory(engine)
    _mark_interrupted_jobs(app.state.session_factory)
    app.state.llm_base = os.environ.get("PTH_LLM_BASE_URL", llm.DEFAULT_BASE)
    app.state.llm_key = os.environ.get("LM_API_TOKEN")
    app.state.llm_transport = None  # тесты подставляют имитацию LM Studio
    app.include_router(router)

    @app.exception_handler(domain.DomainError)
    async def domain_error(_: Request, exc: domain.DomainError):
        return JSONResponse(status_code=exc.status,
                            content={"error": {"code": exc.code, "message": exc.message}})

    return app
