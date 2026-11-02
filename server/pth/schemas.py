"""Контракты API (Pydantic). Попадают в OpenAPI без отдельного описания."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ArtifactType = Literal["US", "FR", "NFR", "AC", "ADR", "COMMIT", "TEST", "DIAGRAM"]
RelationType = Literal["refines", "motivates", "implements", "verifies", "illustrates"]
LostReason = Literal["no_code_link", "no_reflection", "unverified_commit"]
Direction = Literal["out", "in", "both"]


class ProjectIn(BaseModel):
    key: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,9}$", examples=["PTH"])
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    key: str
    name: str
    created_at: datetime


class ArtifactIn(BaseModel):
    type: ArtifactType
    title: str = Field(min_length=1, max_length=300)
    text: str = ""
    attributes: dict = Field(default_factory=dict, examples=[{"priority": "P0"}])
    exemption_reason: str | None = None


class RevisionIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    text: str = ""
    attributes: dict = Field(default_factory=dict)
    exemption_reason: str | None = None
    expected_revision: int | None = Field(
        default=None, description="Номер ревизии, на которую опирается правка; несовпадение даёт 409."
    )


class RevisionOut(BaseModel):
    number: int
    title: str
    text: str
    attributes: dict
    exemption_reason: str | None
    created_at: datetime


class ArtifactOut(BaseModel):
    display_id: str
    type: ArtifactType
    external_key: str | None
    revision: int
    title: str
    text: str
    attributes: dict
    exemption_reason: str | None
    created_at: datetime
    archived_at: datetime | None = None
    archive_reason: str | None = None
    replaced_by: str | None = Field(default=None, description="display_id преемника, если элемент заменён")


class ArchiveIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000, description="почему элемент уходит в архив")
    replaced_by: str | None = Field(default=None, description="display_id элемента, который его заменяет")


class RelationIn(BaseModel):
    type: RelationType
    source: str = Field(description="display_id источника")
    target: str = Field(description="display_id цели")


class RelationOut(BaseModel):
    uid: str
    type: RelationType
    source: str
    target: str
    status: str


class CommitIn(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    title: str = Field(min_length=1, max_length=300)
    implements: list[str] = Field(default_factory=list, description="display_id требований, которые реализует коммит")


class ImportIn(BaseModel):
    source_name: str = Field(min_length=1, max_length=200, examples=["Project_Traceability_System_Spec_v0.1.md"])
    text: str = Field(min_length=1, max_length=10_000_000, description="Markdown принятой спецификации")


class ImportChange(BaseModel):
    display_id: str
    action: Literal["created", "revised"]


class ExtractIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2_000_000)


class Proposal(BaseModel):
    type: Literal["US", "FR", "NFR", "AC"]
    title: str
    text: str
    quote: str
    quote_verified: bool
    model: str


class ExtractOut(BaseModel):
    model: str
    proposals: list[Proposal]
    skipped: int = Field(description="Элементов, которые не прошли проверку формата")


class ExtractionJobIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    source_name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=10_000_000)


class ExtractionJobOut(BaseModel):
    uid: str
    state: Literal["queued", "running", "succeeded", "failed"]
    model: str
    source_name: str
    chunks_total: int
    chunks_done: int
    proposals: list[Proposal]
    skipped: int
    errors: list[str]
    created_at: datetime
    finished_at: datetime | None


class ProposalSource(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)


class ProposalsToChangeSetIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    proposals: list[Proposal] = Field(min_length=1, max_length=500)
    # Текст, из которого модель выделила предложения: по нему сохраняется источник с подсветкой цитат
    source: ProposalSource | None = None


class ChangeSetIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    operations: list[dict] = Field(min_length=1, max_length=1000)


class ChangeSetOut(BaseModel):
    uid: str
    title: str
    state: Literal["draft", "applied"]
    operations: list[dict]
    operation_count: int
    last_error: str | None
    created_at: datetime
    applied_at: datetime | None


class ImportReport(BaseModel):
    uid: str | None = Field(description="None для предпросмотра (dry_run) и черновика")
    changeset_uid: str | None = Field(default=None, description="Заполнено, если импорт создан как черновик")
    source_name: str
    source_sha256: str
    dry_run: bool
    created: int
    revised: int
    unchanged: int
    relations_created: int
    unparsed: list[str] = Field(description="Строки таблиц с ID, которые не удалось разобрать")
    changes: list[ImportChange] = Field(default_factory=list, description="Что создано или изменено")
    created_at: datetime


class ImportDetail(ImportReport):
    source_text: str


class LostItem(BaseModel):
    display_id: str
    type: ArtifactType
    title: str
    priority: str | None
    reasons: list[LostReason]
    last_commit: str | None
    recommended_action: str


class LostOut(BaseModel):
    total: int
    items: list[LostItem]
    excluded: list[str] = Field(description="Элементы с обоснованным исключением: не потеряны, а исключены")


class GraphNode(BaseModel):
    display_id: str
    type: ArtifactType
    title: str
    lost: bool
    not_implemented: bool = False
    level: int = 0  # расстояние от центра графа в связях
    reasons: list[LostReason]
    archived: bool = False  # центр графа в архиве (архивные соседи в граф не попадают)


class GraphEdge(BaseModel):
    type: RelationType
    source: str
    target: str


class GraphOut(BaseModel):
    focus: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool


ImpactType = Literal["US", "FR", "NFR", "AC", "ADR", "TEST", "COMMIT", "DIAGRAM"]


class ImpactRuleIn(BaseModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9-]{2,39}$")
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(default="", max_length=500)
    relation_type: str = Field(pattern=r"^(refines|motivates|implements|verifies|illustrates)$")
    direction: str = Field(pattern=r"^(in|out)$")
    focus_types: list[ImpactType] = Field(default_factory=list, max_length=7)     # что изменилось; пусто — любой
    neighbor_types: list[ImpactType] = Field(default_factory=list, max_length=7)  # что проверяем; пусто — любой
    certainty: str = Field(pattern=r"^(explicit|candidate)$")
    action: str = Field(min_length=3, max_length=200)


class ImpactRulePatch(BaseModel):
    enabled: bool


class CommitLinkIn(BaseModel):
    implements: list[str] = Field(min_length=1, max_length=50)


class ReleaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class ReleaseItemsIn(BaseModel):
    display_ids: list[str] = Field(min_length=1, max_length=200)
    with_related: bool = True  # вместе с уточняющими ФТ, НФТ и AC


class ReleaseReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ReleaseDoIn(BaseModel):
    tag: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=1000)


class ReleaseMoveIn(BaseModel):
    display_id: str
    from_release: str | None = None
    to_release: str | None = None
    with_related: bool = True


class ImportPreviewIn(BaseModel):
    source_name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)


class DraftItemIn(BaseModel):
    ref: str = Field(min_length=1, max_length=80, description="Имя элемента внутри черновика: на него ссылаются связи")
    type: Literal["US", "FR", "NFR", "AC"]
    title: str = Field(min_length=1, max_length=300)
    text: str = ""
    priority: Literal["P0", "P1", "P2", "P3"] | None = None
    display_id: str | None = Field(default=None, description="Существующий элемент — новая ревизия; свободный ID — создание с ним")
    external_key: str | None = None
    number: int | None = None
    line: int | None = Field(default=None, description="Строка таблицы источника (импорт таблиц)")
    cols: list[int] = Field(default_factory=list)
    quote: str | None = Field(default=None, description="Цитата из источника, на которой основан элемент (выделение моделью)")
    quote_verified: bool | None = None
    model: str | None = None


class DraftRelationIn(BaseModel):
    type: RelationType
    source: str = Field(description="ref элемента черновика или display_id существующего")
    target: str


class DraftIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    source: ProposalSource | None = None
    items: list[DraftItemIn] = Field(min_length=1, max_length=1000)
    relations: list[DraftRelationIn] = Field(default_factory=list, max_length=3000)
