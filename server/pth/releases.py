"""Релизы: набор требований, фиксация, продвижение по реализации и проверке, решение о выпуске, карта историй."""
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pth import domain, gitops
from pth.domain import DomainError
from pth.models import ProjectRepo, Release, ReleaseItem, utcnow

SCOPE_TYPES = {"US", "FR", "NFR", "AC"}
LEAF_TYPES = {"FR", "NFR", "AC"}
DONE = {"verified", "excluded"}


def _release(session: Session, key: str, uid: str) -> Release:
    rel = session.get(Release, uid)
    if rel is None or rel.project_key != key:
        raise DomainError(404, "RELEASE_NOT_FOUND", f"Релиз {uid} не найден в проекте {key}")
    return rel


def create_release(session: Session, key: str, name: str, description: str, actor: str) -> Release:
    domain.get_project(session, key)
    name = name.strip()
    if session.scalar(select(Release).where(Release.project_key == key, Release.name == name)):
        raise DomainError(409, "RELEASE_EXISTS", f"Релиз {name} уже есть")
    pos = (session.scalar(select(func.max(Release.position)).where(Release.project_key == key)) or 0) + 1
    rel = Release(project_key=key, name=name, description=description.strip(), status="planned", position=pos,
                  created_by=actor, created_at=utcnow())
    session.add(rel)
    session.flush()
    return rel


def _items(session: Session, rel: Release) -> list[ReleaseItem]:
    return list(session.scalars(select(ReleaseItem).where(ReleaseItem.release_uid == rel.uid).order_by(ReleaseItem.created_at)))


def _children(a, uid: str) -> set[str]:
    """Все требования, уточняющие элемент (refines) напрямую и через цепочку: ФТ и AC под US, AC под ФТ."""
    found: set[str] = set()
    frontier = [uid]
    while frontier:
        cur = frontier.pop()
        for r in a.relations:
            if r.type == "refines" and r.target_uid == cur and r.source_uid not in found and a.by_uid[r.source_uid].type in LEAF_TYPES:
                found.add(r.source_uid)
                frontier.append(r.source_uid)
    return found


def _parent_stories(a, uid: str) -> set[str]:
    """US, которые прямо или через цепочку refines стоят над требованием."""
    found: set[str] = set()
    frontier = [uid]
    while frontier:
        cur = frontier.pop()
        for r in a.relations:
            if r.type == "refines" and r.source_uid == cur and r.target_uid not in found:
                found.add(r.target_uid)
                frontier.append(r.target_uid)
    return {u for u in found if a.by_uid[u].type == "US"}


def add_items(session: Session, key: str, rel: Release, display_ids: list[str], with_related: bool) -> list[dict]:
    if rel.status != "planned":
        raise DomainError(409, "RELEASE_LOCKED", "Состав можно менять, пока релиз в статусе «планируется»")
    a = domain.analyze(session, key)
    have = {i.artifact_uid for i in _items(session, rel)}
    added = []
    for d in display_ids:
        art = domain.require_active(domain.get_artifact(session, key, d))
        if art.type not in SCOPE_TYPES:
            raise DomainError(422, "TYPE_NOT_ALLOWED", f"В релиз берутся US, ФТ, НФТ и AC, а не {art.type}")
        chosen = [(art.uid, "manual")] + ([(u, "related") for u in sorted(_children(a, art.uid))] if with_related else [])
        for uid, via in chosen:
            if uid in have:
                continue
            have.add(uid)
            session.add(ReleaseItem(release_uid=rel.uid, artifact_uid=uid, added_via=via, created_at=utcnow()))
            added.append({"display_id": a.by_uid[uid].display_id, "via": via})
    session.flush()
    return added


def remove_item(session: Session, key: str, rel: Release, display_id: str, with_related: bool = False) -> list[str]:
    if rel.status != "planned":
        raise DomainError(409, "RELEASE_LOCKED", "Состав можно менять, пока релиз в статусе «планируется»")
    a = domain.analyze(session, key)
    art = domain.get_artifact(session, key, display_id)
    uids = {art.uid} | (_children(a, art.uid) if with_related else set())
    removed = []
    for it in _items(session, rel):
        if it.artifact_uid in uids:
            removed.append(a.get(it.artifact_uid).display_id)
            session.delete(it)
    if not removed:
        raise DomainError(404, "ITEM_NOT_IN_RELEASE", f"{display_id} нет в составе релиза")
    session.flush()
    return removed


def freeze(session: Session, key: str, rel: Release) -> None:
    if rel.status != "planned":
        raise DomainError(409, "RELEASE_STATE", "Зафиксировать можно только релиз в статусе «планируется»")
    items = _items(session, rel)
    if not items:
        raise DomainError(422, "RELEASE_EMPTY", "В релизе нет ни одного элемента")
    a = domain.analyze(session, key)
    for it in items:
        it.frozen_revision = a.revisions[it.artifact_uid].number
    rel.status, rel.frozen_at = "frozen", utcnow()
    session.flush()


def unfreeze(session: Session, key: str, rel: Release, reason: str) -> None:
    if rel.status != "frozen":
        raise DomainError(409, "RELEASE_STATE", "Снять фиксацию можно только у зафиксированного релиза")
    if not reason.strip():
        raise DomainError(422, "REASON_REQUIRED", "Нужна причина")
    for it in _items(session, rel):
        it.frozen_revision = None
    rel.status, rel.frozen_at = "planned", None
    session.flush()


def cancel(session: Session, key: str, rel: Release, reason: str, actor: str) -> None:
    if rel.status == "released":
        raise DomainError(409, "RELEASE_STATE", "Выпущенный релиз отменить нельзя")
    if not reason.strip():
        raise DomainError(422, "REASON_REQUIRED", "Нужна причина")
    rel.status, rel.decided_by, rel.decision_reason = "cancelled", actor, reason.strip()
    session.flush()


def progress(session: Session, key: str, rel: Release, release_oid: str | None = None) -> dict:
    """Продвижение по составу: что реализовано коммитами и что проверено тестами.
    С release_oid проверка идёт на этом коммите: коммит должен быть достижим от него, свидетельства — относиться к нему."""
    a = domain.analyze(session, key)
    repo = session.get(ProjectRepo, key)
    comp = {i["display_id"]: i for i in domain.compliance(session, key, head_override=release_oid)["items"]}
    commits_of: dict[str, list] = {}
    for r in a.relations:
        if r.type == "implements":
            commits_of.setdefault(r.target_uid, []).append(a.by_uid[r.source_uid])

    def code_commits(uid: str) -> list[str]:
        out = []
        for c in commits_of.get(uid, []):
            if release_oid and repo is not None and c.external_key and not gitops.is_ancestor(repo.path, c.external_key, release_oid):
                continue
            out.append(c.display_id)
        return out

    items = _items(session, rel)
    in_scope = {i.artifact_uid for i in items}
    rows = []
    for it in items:
        art = a.get(it.artifact_uid)
        rev = a.revisions[art.uid]
        parents = sorted(a.by_uid[u].display_id for u in _parent_stories(a, art.uid)) if art.type in LEAF_TYPES else []
        missing = sorted(a.by_uid[u].display_id for u in _parent_stories(a, art.uid) if u not in in_scope) if art.type in LEAF_TYPES else []
        base = {"display_id": art.display_id, "type": art.type, "title": rev.title, "priority": rev.attributes.get("priority"),
                "revision": rev.number, "frozen_revision": it.frozen_revision, "via": it.added_via,
                "changed": it.frozen_revision is not None and rev.number > it.frozen_revision,
                # Требование взято без user story: ни одной из его US нет в составе (или US у него нет вовсе)
                "without_story": art.type in LEAF_TYPES and (not parents or len(missing) == len(parents)),
                "stories": parents, "stories_missing": missing}
        if art.archived_at is not None:  # убран в архив после включения в релиз — виден, но не считается готовым
            rows.append({**base, "stage": "archived", "code": [], "verification": None, "leaves": 0})
            continue
        if rev.exemption_reason:
            rows.append({**base, "stage": "excluded", "code": [], "verification": "excluded", "leaves": 0})
            continue
        if art.type == "US":
            leaves = [u for u in _children(a, art.uid) if u in in_scope and not a.revisions[u].exemption_reason]
            if not leaves:
                rows.append({**base, "stage": "no_requirements", "code": [], "verification": None, "leaves": 0})
                continue
            codes = [code_commits(u) for u in leaves]
            states = [comp.get(a.by_uid[u].display_id, {}).get("status") for u in leaves]
            all_code = all(codes)
            code = sorted({c for cs in codes for c in cs})
            verification = "verified" if all(s == "verified" for s in states) else ("failed" if "failed" in states else "partial")
            leaf_count = len(leaves)
        else:
            c = code_commits(art.uid)
            code, all_code = c, bool(c)
            verification = comp.get(art.display_id, {}).get("status")
            leaf_count = 0
        if verification == "failed":
            stage = "failed"
        elif all_code and verification == "verified":
            stage = "verified"
        elif all_code:
            stage = "implemented"
        else:
            stage = "not_started"
        rows.append({**base, "stage": stage, "code": code, "verification": verification, "leaves": leaf_count})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
    done = sum(counts.get(s, 0) for s in DONE)
    blockers = [{"display_id": r["display_id"], "stage": r["stage"]} for r in rows if r["stage"] not in DONE]
    return {"items": rows, "counts": counts, "total": len(rows), "done": done,
            "percent": round(100 * done / len(rows)) if rows else 0, "blockers": blockers,
            "changed_after_freeze": [r["display_id"] for r in rows if r["changed"]],
            "without_story": [r["display_id"] for r in rows if r["without_story"]]}


def release_out(session: Session, key: str, rel: Release, with_progress: bool = True) -> dict:
    out = {"uid": rel.uid, "name": rel.name, "description": rel.description, "status": rel.status, "position": rel.position,
           "tag": rel.tag, "commit": rel.commit_oid, "created_by": rel.created_by, "created_at": rel.created_at.isoformat(),
           "frozen_at": rel.frozen_at.isoformat() if rel.frozen_at else None,
           "released_at": rel.released_at.isoformat() if rel.released_at else None,
           "decided_by": rel.decided_by, "decision_reason": rel.decision_reason, "readiness": rel.readiness}
    if with_progress:
        out["progress"] = (rel.readiness if rel.status == "released" and rel.readiness else progress(session, key, rel))
    return out


def list_releases(session: Session, key: str) -> list[dict]:
    domain.get_project(session, key)
    rels = session.scalars(select(Release).where(Release.project_key == key).order_by(Release.position))
    out = []
    for r in rels:
        d = release_out(session, key, r)
        p = d["progress"]
        d["progress"] = {"total": p["total"], "done": p["done"], "percent": p["percent"], "counts": p["counts"]}
        out.append(d)
    return out


def do_release(session: Session, key: str, rel: Release, tag: str, reason: str, actor: str) -> dict:
    """Выпуск: тег должен существовать в подключённом репозитории. Готовность считается на коммите тега.
    Если есть незакрытые элементы, выпуск возможен только с причиной."""
    if rel.status != "frozen":
        raise DomainError(409, "RELEASE_STATE", "Выпустить можно только зафиксированный релиз")
    repo = session.get(ProjectRepo, key)
    if repo is None:
        raise DomainError(409, "REPO_NOT_CONNECTED", "Для выпуска нужен подключённый репозиторий с тегом")
    oid = gitops.resolve_tag(repo.path, tag.strip())
    if oid is None:
        raise DomainError(422, "TAG_NOT_FOUND", f"Тег {tag} не найден в репозитории")
    taken = session.scalar(select(Release).where(Release.project_key == key, Release.status == "released", Release.tag == tag.strip()))
    if taken is not None:
        raise DomainError(409, "TAG_USED", f"Тег {tag} уже использован релизом {taken.name}")
    snap = progress(session, key, rel, release_oid=oid)
    if snap["blockers"] and not reason.strip():
        raise DomainError(409, "BLOCKERS_PRESENT",
                          f"Не закрыто элементов: {len(snap['blockers'])}. Выпуск с блокерами требует причину.")
    info = gitops.show_commit(repo.path, oid)
    if info:
        domain.ensure_commit(session, key, oid, info["subject"], info["author"], info["authored_at"])
    rel.status, rel.tag, rel.commit_oid = "released", tag.strip(), oid
    rel.released_at, rel.decided_by, rel.decision_reason, rel.readiness = utcnow(), actor, reason.strip() or None, snap
    session.flush()
    return snap


# --- карта историй (USM) -----------------------------------------------------------

ROLE = re.compile(r"^\s*Как\s+(.+?),\s*хочу", re.IGNORECASE)


def story_map(session: Session, key: str) -> dict:
    """Карта историй: колонки — активности (атрибут activity у US, иначе роль из «Как <роль>, хочу…»),
    строки — релизы по порядку и «не запланировано». Под каждой US — уточняющие её ФТ."""
    domain.get_project(session, key)
    a = domain.analyze(session, key)
    comp = {i["display_id"]: i["status"] for i in domain.compliance(session, key)["items"]}
    coded = {r.target_uid for r in a.relations if r.type == "implements"}
    rels = list(session.scalars(select(Release).where(Release.project_key == key, Release.status != "cancelled").order_by(Release.position)))
    where: dict[str, str] = {}
    for rel in rels:
        for it in _items(session, rel):
            where.setdefault(it.artifact_uid, rel.uid)
    in_release: dict[str, set[str]] = {}
    for rel in rels:
        in_release[rel.uid] = {i.artifact_uid for i in _items(session, rel)}
    cards = []
    for art in sorted((x for x in a.by_uid.values() if x.type == "US"), key=lambda x: x.display_id):
        rev = a.revisions[art.uid]
        children = _children(a, art.uid)
        taken = len(children & in_release.get(where.get(art.uid, ""), set()))
        role = ROLE.match(rev.title)
        activity = rev.attributes.get("activity") or (role.group(1).strip() if role else "Без активности")
        frs = [x for x in (a.by_uid[r.source_uid] for r in a.relations if r.type == "refines" and r.target_uid == art.uid)
               if x.type == "FR"]
        cards.append({"display_id": art.display_id, "title": rev.title, "priority": rev.attributes.get("priority"),
                      "activity": activity, "release": where.get(art.uid),
                      # С зависимостями: сколько уточняющих требований US взято в тот же релиз из всех её требований
                      "dependencies": {"taken": taken, "total": len(children)} if art.uid in where else None,
                      "requirements": [{"display_id": f.display_id, "title": a.revisions[f.uid].title,
                                        "implemented": f.uid in coded, "status": comp.get(f.display_id)}
                                       for f in sorted(frs, key=lambda x: x.display_id)]})
    order = {}
    for c in cards:
        order.setdefault(c["activity"], len(order))
    cards.sort(key=lambda c: (order[c["activity"]], c["priority"] or "P9", c["display_id"]))
    return {"activities": list(order), "releases": [{"uid": r.uid, "name": r.name, "status": r.status} for r in rels], "cards": cards}


def move_item(session: Session, key: str, display_id: str, from_uid: str | None, to_uid: str | None, with_related: bool = True) -> dict:
    """Перенос элемента между релизами (None — «не запланировано»). Оба релиза должны быть в статусе «планируется»."""
    if from_uid == to_uid:
        return {"removed": [], "added": []}
    to_rel = _release(session, key, to_uid) if to_uid else None
    from_rel = _release(session, key, from_uid) if from_uid else None
    for rel in (from_rel, to_rel):
        if rel is not None and rel.status != "planned":
            raise DomainError(409, "RELEASE_LOCKED", f"Релиз {rel.name} зафиксирован или закрыт: состав менять нельзя")
    removed = remove_item(session, key, from_rel, display_id, with_related) if from_rel else []
    added = add_items(session, key, to_rel, [display_id], with_related) if to_rel else []
    return {"removed": removed, "added": [x["display_id"] for x in added]}


def compare(session: Session, key: str, a_uid: str, b_uid: str) -> dict:
    """Чем отличается состав двух релизов: что добавлено, убрано и у чего сменилась ревизия."""
    ra, rb = _release(session, key, a_uid), _release(session, key, b_uid)
    a = domain.analyze(session, key)

    def snap(rel: Release) -> dict[str, tuple[int, str]]:
        return {a.get(i.artifact_uid).display_id: (i.frozen_revision or a.revisions[i.artifact_uid].number,
                                                      a.revisions[i.artifact_uid].title) for i in _items(session, rel)}
    sa, sb = snap(ra), snap(rb)
    row = lambda d, s: {"display_id": d, "title": s[d][1], "revision": s[d][0]}  # noqa: E731
    return {"a": {"uid": ra.uid, "name": ra.name}, "b": {"uid": rb.uid, "name": rb.name},
            "added": [row(d, sb) for d in sorted(sb.keys() - sa.keys())],
            "removed": [row(d, sa) for d in sorted(sa.keys() - sb.keys())],
            "changed": [{"display_id": d, "title": sb[d][1], "from": sa[d][0], "to": sb[d][0]}
                        for d in sorted(sa.keys() & sb.keys()) if sa[d][0] != sb[d][0]],
            "same": len(sa.keys() & sb.keys())}


STAGE_RU = {"verified": "реализовано и проверено", "implemented": "реализовано, не проверено", "not_started": "не реализовано",
            "failed": "проверка упала", "excluded": "исключено", "no_requirements": "нет ФТ в составе"}
STATUS_RU = {"planned": "планируется", "frozen": "зафиксирован", "released": "выпущен", "cancelled": "отменён"}


def report_markdown(session: Session, key: str, rel: Release) -> str:
    """Отчёт по релизу: цель, решение, состав со стадиями, незакрытое, проверки по требованиям, коммиты реализации."""
    prog = rel.readiness if rel.status == "released" and rel.readiness else progress(session, key, rel)
    a = domain.analyze(session, key)
    comp = {i["display_id"]: i for i in domain.compliance(session, key)["items"]}
    fmt = lambda d: d.strftime("%d.%m.%Y %H:%M") if d else "—"  # noqa: E731
    cell = lambda s: " ".join(str(s).split()).replace("|", "\\|")  # noqa: E731
    lines = [f"# Релиз {rel.name} — {key}", "", f"**Статус:** {STATUS_RU[rel.status]}.", ""]
    if rel.description:
        lines += [rel.description, ""]
    lines += ["| Что | Значение |", "| --- | --- |", f"| Создан | {fmt(rel.created_at)}, {rel.created_by} |",
              f"| Состав зафиксирован | {fmt(rel.frozen_at)} |", f"| Тег | {rel.tag or '—'} |",
              f"| Коммит | {(rel.commit_oid or '—')[:12]} |", f"| Выпущен | {fmt(rel.released_at)} |",
              f"| Решение принял | {rel.decided_by or '—'} |"]
    if rel.decision_reason:
        lines += ["", f"**Причина и комментарий:** {rel.decision_reason}"]
    lines += ["", "## Итог", "", f"Готово {prog['done']} из {prog['total']} ({prog['percent']}%).", "",
              "| Стадия | Элементов |", "| --- | --- |"]
    lines += [f"| {STAGE_RU.get(s, s)} | {n} |" for s, n in sorted(prog["counts"].items(), key=lambda x: -x[1])] or ["| состав пуст | 0 |"]
    lines += ["", "## Состав", "", "| Элемент | Название | Приоритет | Стадия | Коммиты |", "| --- | --- | --- | --- | --- |"]
    for it in prog["items"]:
        lines.append(f"| {it['display_id']} | {cell(it['title'])} | {it['priority'] or '—'} | {STAGE_RU.get(it['stage'], it['stage'])} | {', '.join(it['code']) or '—'} |")
    lines += ["", "## Не закрыто", ""]
    lines += [f"- {b['display_id']}: {STAGE_RU.get(b['stage'], b['stage'])}" for b in prog["blockers"]] or ["- ничего"]
    if prog.get("changed_after_freeze"):
        lines += ["", "## Изменено после фиксации", ""] + [f"- {d}" for d in prog["changed_after_freeze"]]
    lines += ["", "## Проверки требований", "", "| Требование | Статус | Тесты |", "| --- | --- | --- |"]
    for it in prog["items"]:
        c = comp.get(it["display_id"])
        if c:
            tests = ", ".join(f"{x['display_id']} ({domain.STATUS_RU.get(x['status'], x['status'])})" for x in c["tests"]) or "—"
            lines.append(f"| {it['display_id']} | {domain.STATUS_RU.get(c['status'], c['status'])} | {tests} |")
    lines += ["", "## Ограничения отчёта", "",
              "- Реализация — это заявленная авторами связь коммита с требованием, а не проверка кода.",
              "- Для выпущенного релиза готовность взята из снимка на момент выпуска и не пересчитывается."]
    return "\n".join(lines) + "\n"
