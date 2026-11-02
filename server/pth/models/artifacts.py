import re
from datetime import datetime
from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from pth.db import Base
from pth.models.common import new_uid, utcnow


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("project_key", "display_id"),
        UniqueConstraint("project_key", "external_key"),
        # Название TEST уникально в проекте: проверка «тест уже заведён?» в коде не закрывает гонку.
        Index("uq_artifacts_test_title", "project_key", "type", "title_key", unique=True),
    )

    uid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uid)
    project_key: Mapped[str] = mapped_column(ForeignKey("projects.key"), index=True)
    type: Mapped[str] = mapped_column(String(10))
    number: Mapped[int] = mapped_column(Integer)
    display_id: Mapped[str] = mapped_column(String(64))
    # Внешний ключ для COMMIT (SHA); у остальных типов пусто.
    external_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Нормализованное текущее название — только у TEST (у остальных пусто). Ведёт _sync_test_title_keys.
    title_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Архив вместо удаления (спека §5.4): данные, ревизии и ID сохраняются; восстановление — новой ревизией.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archive_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    replaced_by_uid: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ArtifactSequence(Base):
    """Последний выданный номер артефакта по типу. Номер берётся атомарным UPDATE … RETURNING: параллельные
    записи ждут друг друга, а не получают один номер; номера удалённых артефактов повторно не выдаются."""
    __tablename__ = "artifact_sequences"

    project_key: Mapped[str] = mapped_column(ForeignKey("projects.key"), primary_key=True)
    type: Mapped[str] = mapped_column(String(10), primary_key=True)
    last: Mapped[int] = mapped_column(Integer, default=0)


class ArtifactRevision(Base):
    __tablename__ = "artifact_revisions"
    __table_args__ = (UniqueConstraint("artifact_uid", "number"),)

    uid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uid)
    artifact_uid: Mapped[str] = mapped_column(ForeignKey("artifacts.uid"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text, default="")
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    # Обоснованное исключение из проверки (PTH-US-045): выводит элемент из списка потерянных.
    exemption_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Relation(Base):
    __tablename__ = "relations"
    __table_args__ = (UniqueConstraint("type", "source_uid", "target_uid"),)

    uid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uid)
    project_key: Mapped[str] = mapped_column(ForeignKey("projects.key"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    source_uid: Mapped[str] = mapped_column(ForeignKey("artifacts.uid"), index=True)
    target_uid: Mapped[str] = mapped_column(ForeignKey("artifacts.uid"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Связь в архиве (ChangeSet delete_relation): в анализе не участвует, повторное создание её возвращает.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def test_title_key(title: str) -> str:
    """Ключ уникальности названия TEST: пробелы по краям убраны, подряд идущие схлопнуты; регистр важен."""
    return re.sub(r"\s+", " ", title).strip()


@event.listens_for(Session, "before_flush")
def _sync_test_title_keys(session: Session, _context, _instances) -> None:
    """Каждая новая ревизия TEST (создание, правка, ChangeSet) обновляет title_key артефакта.
    Занятое название — DomainError 409 с ID существующего теста; гонку параллельных запросов
    закрывает уникальный индекс uq_artifacts_test_title."""
    revisions = [o for o in session.new if isinstance(o, ArtifactRevision)]
    if not revisions:
        return
    pending = {o.uid: o for o in session.new if isinstance(o, Artifact)}
    claimed: dict[tuple[str, str], str] = {}  # ключи, занятые в этой же записи
    with session.no_autoflush:
        for rev in sorted(revisions, key=lambda r: r.number):
            art = pending.get(rev.artifact_uid) or session.get(Artifact, rev.artifact_uid)
            if art is None or art.type != "TEST":
                continue
            key = test_title_key(rev.title)
            other = claimed.get((art.project_key, key)) or session.scalar(
                select(Artifact.display_id).where(Artifact.project_key == art.project_key, Artifact.type == "TEST",
                                                  Artifact.title_key == key, Artifact.uid != art.uid))
            if other and other != art.display_id:
                from pth.domain import DomainError
                raise DomainError(409, "TEST_EXISTS", f"TEST с названием «{key}» уже есть: {other}")
            art.title_key = key
            claimed[(art.project_key, key)] = art.display_id
