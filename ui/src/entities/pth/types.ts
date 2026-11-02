/** Типы ответов API PTH. Соответствуют server/openapi.json (схемы LostOut, GraphOut, ArtifactOut). */

export type ArtifactType = 'US' | 'FR' | 'NFR' | 'AC' | 'ADR' | 'COMMIT' | 'TEST' | 'DIAGRAM';
export type RelationType = 'refines' | 'motivates' | 'implements' | 'verifies' | 'illustrates';
export type LostReason = 'no_code_link' | 'no_reflection' | 'unverified_commit';

export interface LostItem {
  display_id: string;
  type: ArtifactType;
  title: string;
  priority: string | null;
  reasons: LostReason[];
  last_commit: string | null;
  recommended_action: string;
}

export interface LostList {
  total: number;
  items: LostItem[];
  /** Элементы с обоснованным исключением: не потеряны, а исключены. */
  excluded: string[];
}

export interface RevisionItem {
  number: number;
  title: string;
  text: string;
  attributes: Record<string, unknown>;
  exemption_reason: string | null;
  created_at: string;
}

export interface GraphNode {
  display_id: string;
  type: ArtifactType | 'GROUP';
  /** Свёрнутый уровень: вместо узлов один счётчик. */
  collapsed?: { level: number; count: number; deeper: number };
  title: string;
  /** Потерян: user story без ФТ или ADR. */
  lost: boolean;
  /** Не реализован: требование без связи implements. Потерянным не считается. */
  not_implemented: boolean;
  reasons: LostReason[];
  /** Расстояние от центра графа в связях. */
  level?: number;
}

export interface GraphEdge {
  type: RelationType;
  source: string;
  target: string;
}

export interface GraphData {
  focus: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
}

export interface ArtifactDetail {
  display_id: string;
  type: ArtifactType;
  external_key: string | null;
  revision: number;
  title: string;
  text: string;
  attributes: Record<string, unknown>;
  exemption_reason: string | null;
  created_at: string;
}

export interface ImportChange {
  display_id: string;
  action: 'created' | 'revised';
}

export interface ImportReport {
  uid: string | null;
  changeset_uid: string | null;
  source_name: string;
  source_sha256: string;
  dry_run: boolean;
  created: number;
  revised: number;
  unchanged: number;
  relations_created: number;
  unparsed: string[];
  changes: ImportChange[];
  created_at: string;
}

export type ImportMode = 'dry_run' | 'changeset' | 'apply';

export interface Proposal {
  type: 'US' | 'FR' | 'NFR' | 'AC';
  title: string;
  text: string;
  quote: string;
  quote_verified: boolean;
  model: string;
}

export interface ExtractResult {
  model: string;
  proposals: Proposal[];
  skipped: number;
}

export interface ExtractionJob {
  uid: string;
  state: 'queued' | 'running' | 'succeeded' | 'failed';
  model: string;
  source_name: string;
  chunks_total: number;
  chunks_done: number;
  proposals: Proposal[];
  skipped: number;
  errors: string[];
  created_at: string;
  finished_at: string | null;
}

export interface ProjectInfo {
  key: string;
  name: string;
  created_at: string;
}

export interface ChangeSet {
  uid: string;
  title: string;
  state: 'draft' | 'applied';
  operations: Record<string, unknown>[];
  operation_count: number;
  last_error: string | null;
  created_at: string;
  applied_at: string | null;
}

export interface ImportDetail extends ImportReport {
  source_text: string;
}

/** Два разных состояния, не один список: потерянный элемент и нереализованный. */
export const REASON_LABEL: Record<LostReason, string> = {
  no_code_link: 'Не реализован: нет связи с кодом (implements)',
  no_reflection: 'Потерян: user story без ФТ или ADR',
  unverified_commit: 'Связь с кодом не подтверждена: SHA коммита не проверен по Git',
};

export interface StatusSummary {
  artifacts: Record<string, number>;
  lost: number;
  excluded: number;
  draft_changesets: number;
  imports: number;
  relations: number;
}

export interface ReviewFinding {
  severity: 'error' | 'warning' | 'info';
  rule: string;
  display_id: string;
  message: string;
  state: 'open' | 'resolved' | 'dismissed' | 'reopened';
  decision: { state: string; reason: string; actor: string; revision: number } | null;
}

export interface ImpactItem {
  display_id: string;
  type: ArtifactType;
  depth: number;
  path: string[];
  rule_id: string;
  /** Все правила, которые затронули элемент: встроенные и свои. */
  rule_ids?: string[];
  certainty: 'explicit' | 'candidate';
  action: string;
}

/** Пояснение правила проверки качества: что проверяет и почему это важно. */
export interface ReviewRuleText {
  severity: 'error' | 'warning' | 'info';
  title: string;
  description: string;
}

/** Пояснение правила, которое сработало в ответе влияния. */
export interface ImpactRuleText {
  title: string;
  description: string;
}

export interface ImpactRuleRow {
  code: string;
  title: string;
  description: string;
  relation_type: 'refines' | 'motivates' | 'implements' | 'verifies';
  direction: 'in' | 'out';
  /** Что изменилось: типы, которые может иметь изменившийся элемент (по связи и направлению). */
  focus_types: string[];
  /** Что проверяем: типы соседа. */
  neighbor_types: string[];
  certainty: 'explicit' | 'candidate';
  action: string;
  enabled: boolean;
}

/** Тело создания своего правила. */
export interface ImpactRuleInput {
  code: string;
  title: string;
  description: string;
  relation_type: string;
  direction: string;
  /** Пустой список — любой допустимый по связи тип. */
  focus_types: string[];
  neighbor_types: string[];
  certainty: string;
  action: string;
}

export interface BuiltinImpactRule {
  code: string;
  title: string;
  description: string;
  relation_type: string;
  direction: string;
  focus_types: string[];
  neighbor_types: string[];
  certainty: string;
  action: string;
}

export interface ImpactRules {
  builtin: BuiltinImpactRule[];
  custom: ImpactRuleRow[];
}

export interface ComplianceItem {
  display_id: string;
  status: 'verified' | 'partial' | 'failed' | 'stale' | 'not_verified' | 'no_test' | 'excluded';
  tests: { display_id: string; status: string }[];
}

export interface Readiness {
  verdict: 'ready' | 'not_ready' | 'insufficient_data';
  total: number;
  blocking: { display_id: string; status: string }[];
  counts: Record<string, number>;
}

export interface EvidenceRow {
  trust_level: string;
  verified: boolean;
  issuer: string | null;
  uid: string;
  test: string | null;
  result: 'pass' | 'fail' | 'blocked' | 'inconclusive';
  commit_sha: string | null;
  environment: Record<string, unknown>;
  created_at: string;
}

export interface CommitRow {
  display_id: string;
  sha: string | null;
  title: string;
  implements: string[];
  created_at: string;
}

export interface KeyRow {
  uid: string;
  name: string;
  kind: string;
  project_key: string | null;
  role: string | null;
  revoked: boolean;
  created_at: string;
}

export interface Me {
  name: string;
  kind: string;
  project_key: string | null;
  role: string | null;
}

export interface ArtifactRow {
  display_id: string;
  type: ArtifactType;
  external_key: string | null;
  revision: number;
  title: string;
  text: string;
  attributes: Record<string, unknown>;
  exemption_reason: string | null;
  created_at: string;
}

export interface RelationRow {
  uid: string;
  type: RelationType;
  source: string;
  target: string;
  status: string;
}

export interface SourceLine {
  no: number;
  text: string;
  display_id: string | null;
}

/** Формулировка источника, из которой получено требование. via: table — строка таблицы, llm — цитата модели. */
export interface SourceMark {
  display_id: string;
  line: number;
  end_line: number;
  via: 'table' | 'llm';
  /** Для строк таблицы: индексы ячеек с формулировкой. */
  cols: number[];
  quote?: string | null;
  verified?: boolean | null;
  start_col?: number | null;
  end_col?: number | null;
}

export interface SourceView {
  source_name: string;
  source_sha256: string;
  created_at: string;
  lines: SourceLine[];
  fragments: number;
  marks: SourceMark[];
}

export interface AuditRow {
  actor: string;
  action: string;
  target: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface CommitDetail extends CommitRow {
  evidence: EvidenceRow[];
  reachability: string;
}

export interface TestRun {
  result: string;
  commit: string | null;
  ran_at: string;
  trust: string;
  verified: boolean;
  issuer: string | null;
}

/** Тест с составом, проверяемыми требованиями, кодом и историей запусков. */
export interface TestRow {
  test: string;
  title: string;
  text: string;
  command: string | null;
  kind: string;
  artifacts: string | null;
  files: string[];
  selector: string | null;
  coverage: 'full' | 'partial';
  coverage_gaps: string | null;
  requirements: string[];
  requirement_titles: Record<string, string>;
  code_commits: { display_id: string; sha: string | null; title: string }[];
  /** История файлов теста в Git; null — репозиторий не подключён или в команде нет файлов. */
  test_history: { added: GitCommit | null; changes: GitCommit[] } | null;
  state: string;
  commit: string | null;
  result: string | null;
  ran_at: string | null;
  history: TestRun[];
}

/** Что произошло на коммите: запуски тестов и реализуемые требования. */
export interface CommitRuns {
  sha: string | null;
  registered: string | null;
  title: string | null;
  implements: string[];
  runs: { files: { uid: string; name: string; size: number; content_type: string }[]; test: string | null; title: string | null; result: string; ran_at: string; trust: string; verified: boolean; issuer: string | null }[];
}

export interface GitCommit {
  sha: string;
  title: string;
  date: string;
}

/** Набор запусков: все проверки, выполненные на одном коммите. По каждому тесту — последний запуск. */
export interface RunSet {
  commit: string | null;
  ran_at: string;
  total: number;
  passed: number;
  failed: number;
  other: number;
  kinds: string[];
  attachments: number;
  sources: string[];
  results: { test: string | null; title: string | null; kind: string; result: string; ran_at: string; trust: string; verified: boolean; issuer: string | null; attachments: number }[];
}

/** Просмотр файла запуска: текст и сводка по формату (Allure, SARIF, quality gate). */
export interface AttachmentPreview {
  uid: string;
  name: string;
  size: number;
  sha256: string;
  content_type: string;
  text: string | null;
  truncated: boolean;
  summary:
    | { type: 'allure'; total: number; counts: Record<string, number>; seconds: number; failed: string[] }
    | { type: 'sarif'; tools: string[]; levels: Record<string, number>; total: number; top_rules: [string, number][] }
    | { type: 'quality_gate'; status: string; failed: string[] }
    | null;
}

/** Коммит из Git с данными PTH. Регистрация в PTH не требуется. */
export interface GitCommitRow {
  sha: string;
  author: string;
  date: string;
  title: string;
  parents: string[];
  registered: string | null;
  implements: string[];
  mentions: string[];
  checks: { total: number; passed: number; failed: number } | null;
}

export interface GitCommitDetail {
  sha: string;
  author: string;
  email: string;
  authored_at: string;
  committer: string;
  committed_at: string;
  parents: string[];
  subject: string;
  body: string;
  tags: string[];
  files: { path: string; added: number | null; deleted: number | null; status: string }[];
  is_head: boolean;
  registered: string | null;
  implements: { id: string; title: string }[];
  mentions: { id: string; title: string }[];
  runs: CommitRuns['runs'];
}

export interface DiffLine {
  type: 'ctx' | 'add' | 'del';
  old: number | null;
  new: number | null;
  text: string;
}

export interface FileDiff {
  path: string;
  binary: boolean;
  truncated: boolean;
  hunks: { header: string; section: string; lines: DiffLine[] }[];
}

export interface GitInfo {
  path: string;
  branch: string | null;
  commits: number;
  first_commit_at: string | null;
  head: { sha: string; title: string; author: string; date: string } | null;
  branches: string[];
  tags: string[];
  remotes: { name: string; url: string }[];
  authors: { name: string; commits: number }[];
  files: number;
  top_dirs: { path: string; files: number }[];
  extensions: { ext: string; files: number }[];
}

export type ReleaseStage = 'not_started' | 'implemented' | 'verified' | 'failed' | 'excluded' | 'no_requirements';

export interface ReleaseProgress {
  items: {
    display_id: string; type: string; title: string; priority: string | null; revision: number; frozen_revision: number | null;
    via: string; changed: boolean; stage: ReleaseStage; code: string[]; verification: string | null; leaves: number;
    /** Требование взято без user story: ни одной из его US нет в составе. */
    without_story: boolean; stories: string[]; stories_missing: string[];
  }[];
  counts: Record<string, number>;
  total: number;
  done: number;
  percent: number;
  blockers: { display_id: string; stage: string }[];
  changed_after_freeze: string[];
  without_story: string[];
}

export interface ReleaseDetail {
  uid: string;
  name: string;
  description: string;
  status: 'planned' | 'frozen' | 'released' | 'cancelled';
  position: number;
  tag: string | null;
  commit: string | null;
  created_by: string;
  created_at: string;
  frozen_at: string | null;
  released_at: string | null;
  decided_by: string | null;
  decision_reason: string | null;
  readiness: ReleaseProgress | null;
  progress: ReleaseProgress;
}

export interface ReleaseSummary extends Omit<ReleaseDetail, 'progress'> {
  progress: { total: number; done: number; percent: number; counts: Record<string, number> };
}

export interface StoryMap {
  activities: string[];
  releases: { uid: string; name: string; status: string }[];
  cards: {
    display_id: string; title: string; priority: string | null; activity: string; release: string | null;
    /** Сколько уточняющих требований US взято в тот же релиз; null — US не в релизе. */
    dependencies: { taken: number; total: number } | null;
    requirements: { display_id: string; title: string; implemented: boolean; status: string | null }[];
  }[];
}

export interface ReleaseComparison {
  a: { uid: string; name: string };
  b: { uid: string; name: string };
  added: { display_id: string; title: string; revision: number }[];
  removed: { display_id: string; title: string; revision: number }[];
  changed: { display_id: string; title: string; from: number; to: number }[];
  same: number;
}

/** Откуда получено требование: документ импорта и строка с формулировкой. */
export interface Origin {
  import_uid: string;
  source_name: string;
  imported_at: string;
  line: number;
  end_line: number;
  via: 'table' | 'llm';
  verified: boolean | null;
  text: string;
}

/** Построчное сравнение двух версий источника. ids — требования, чьи формулировки стоят на строке. */
export interface SourceCompare {
  a: { uid: string; source_name: string; created_at: string };
  b: { uid: string; source_name: string; created_at: string };
  identical: boolean;
  hunks: { header: string; lines: { type: 'ctx' | 'add' | 'del'; old: number | null; new: number | null; text: string; ids: string[] }[] }[];
  added_lines: number;
  removed_lines: number;
  requirements: { changed: string[]; removed: string[]; added: string[] };
}

/** Операция ChangeSet человеческим языком и результат проверки применения. */
export interface ChangeOp {
  index: number;
  kind: 'create' | 'revise' | 'link' | 'unlink' | string | undefined;
  status?: 'ok' | 'error' | 'skipped';
  error?: string;
  code?: string;
  display_id?: string;
  type?: string;
  title?: string;
  text?: string;
  priority?: string | null;
  external_key?: string | null;
  provenance?: { quote?: string; quote_verified?: boolean; model?: string } | null;
  before?: { title: string; text: string; priority: string | null; revision: number } | null;
  expected_revision?: number | null;
  source?: string;
  target?: string;
  source_title?: string | null;
  target_title?: string | null;
}

export interface ChangeSetReview {
  changeset: ChangeSet;
  operations: ChangeOp[];
  counts: Record<string, number>;
  /** Проверялось ли применение: только у черновиков. */
  checked: boolean;
  valid: boolean;
  first_error: ChangeOp | null;
  source: {
    name: string; sha256: string; lines: number; via: string; text: string | null;
    fragments: { display_id: string; line: number; end_line: number; via: 'table' | 'llm'; cols: number[]; quote?: string; verified?: boolean; start_col?: number; end_col?: number }[];
  } | null;
}
