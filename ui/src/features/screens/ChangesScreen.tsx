import { useState } from 'react';
import { pthApi } from '@/shared/api/pthApi';
import { useAsync } from '@/shared/lib/useAsync';
import { Badge, Button, InlineAlert, Tabs, TextareaField } from '@/shared/ui';
import { DataTable, Loaded, Section, StatusText, usePaged } from './common';
import { Diff } from './RevisionHistory';
import { CopyId } from '@/shared/ui/CopyId';
import { SourcePane } from '@/features/import/SourcePane';
import type { ChangeOp, ChangeSet, ChangeSetReview } from '@/entities/pth/types';

const dateTime = (iso: string) => new Date(iso).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
const KIND_LABEL: Record<string, string> = { create: 'создаётся', revise: 'новая ревизия', link: 'связь', unlink: 'удаление связи', archive: 'в архив', restore: 'из архива' };
const COUNT_LABEL: Record<string, [string, string, string]> = {
  create: ['элемент', 'элемента', 'элементов'], revise: ['ревизия', 'ревизии', 'ревизий'], link: ['связь', 'связи', 'связей'], unlink: ['удаление связи', 'удаления связей', 'удалений связей'],
  archive: ['в архив', 'в архив', 'в архив'], restore: ['из архива', 'из архива', 'из архива'],
};
const plural = (n: number, w: [string, string, string]) => { const m = n % 100; const d = n % 10; return `${n} ${m >= 11 && m <= 14 ? w[2] : d === 1 ? w[0] : d >= 2 && d <= 4 ? w[1] : w[2]}`; };
const REL_LABEL: Record<string, string> = { refines: 'уточняет', motivates: 'опирается на решение', implements: 'реализует', verifies: 'проверяет', illustrates: 'иллюстрирует' };
const errText = (e: unknown, f: string) => (e instanceof Error ? e.message : f);

/** Изменения (ChangeSet): пакеты операций, которые применяются целиком. Здесь их можно прочитать, проверить, применить или отклонить. */
export function ChangesScreen({ projectKey }: { projectKey: string }) {
  const [open, setOpen] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  if (open) return <ChangeView projectKey={projectKey} uid={open} onBack={() => { setOpen(null); setVersion((v) => v + 1); }} />;
  return <ChangeList projectKey={projectKey} version={version} onOpen={setOpen} />;
}

function Mechanism() {
  return (
    <details open className="rounded-card border border-border bg-surface p-4 text-ui">
      <summary className="cursor-pointer font-semibold text-text">Как работают изменения (ChangeSet)</summary>
      <div className="mt-3 flex flex-col gap-2 text-text">
        <p>ChangeSet — это пакет операций над требованиями: создать элемент, добавить новую ревизию, создать или удалить связь. Любое значимое изменение сначала становится черновиком, а в проект попадает только при применении.</p>
        <ol className="flex list-decimal flex-col gap-1.5 pl-5">
          <li><strong>Откуда берутся.</strong> Импорт таблиц, выделение моделью, правка карточки, SDK и скиллы Claude.</li>
          <li><strong>Черновик.</strong> Ничего в проекте не меняет. Его можно прочитать (что создаётся, чем отличается новая ревизия, какие связи) и отклонить.</li>
          <li><strong>Проверка.</strong> Сервер прогоняет операции вхолостую и откатывает: видно, пройдёт ли применение и на какой операции оно упадёт.</li>
          <li><strong>Применение — всё или ничего.</strong> Если одна операция не проходит, не записывается ничего. Номер ревизии ставит сервер, поэтому устаревшая правка даёт конфликт, а не затирает чужую.</li>
          <li><strong>После применения</strong> ChangeSet неизменяем и остаётся в истории; отменить его нельзя, исправляется новым изменением.</li>
        </ol>
      </div>
    </details>
  );
}

function ChangeList({ projectKey, version, onOpen }: { projectKey: string; version: number; onOpen: (uid: string) => void }) {
  const list = useAsync(`changes:${projectKey}:${version}`, async () => (await pthApi.changesetsPage(projectKey, 0, 500)).items);
  const [state, setState] = useState<'all' | 'draft' | 'applied'>('all');
  return (
    <Section title="Изменения" hint="Черновики и применённые пакеты изменений проекта.">
      <Mechanism />
      <Loaded state={list} retry={list.reload} empty={{ title: 'Изменений пока нет', description: 'Они появляются при импорте, выделении моделью, правке карточек и из SDK.', isEmpty: (d) => d.length === 0 }}>
        {(all) => {
          const rows = [...all].reverse().filter((c) => state === 'all' || c.state === state);
          return (
            <>
              <div role="group" aria-label="Состояние" className="flex flex-wrap gap-2">
                {([['all', 'Все'], ['draft', 'Черновики'], ['applied', 'Применённые']] as const).map(([s, label]) => (
                  <Button key={s} size="sm" variant={state === s ? 'primary' : 'secondary'} aria-pressed={state === s} onClick={() => setState(s)}>
                    {label} {s === 'all' ? all.length : all.filter((c) => c.state === s).length}
                  </Button>
                ))}
              </div>
              <ChangeTable rows={rows} onOpen={onOpen} />
            </>
          );
        }}
      </Loaded>
    </Section>
  );
}

function ChangeTable({ rows, onOpen }: { rows: ChangeSet[]; onOpen: (uid: string) => void }) {
  const paged = usePaged(rows, 20, 'Страницы изменений');
  const kindCounts = (cs: ChangeSet) => {
    const c: Record<string, number> = {};
    for (const op of cs.operations) {
      const k = op.op === 'create_artifact' ? 'create' : op.op === 'add_revision' ? 'revise' : op.op === 'create_relation' ? 'link' : op.op === 'delete_relation' ? 'unlink' : op.op === 'archive_artifact' ? 'archive' : op.op === 'restore_artifact' ? 'restore' : 'other';
      c[k] = (c[k] ?? 0) + 1;
    }
    return Object.entries(c).filter(([k]) => k !== 'other').map(([k, n]) => plural(n, COUNT_LABEL[k])).join(' · ') || `${cs.operation_count} операций`;
  };
  if (rows.length === 0) return <p className="text-ui text-text-muted">В этом разделе ничего нет.</p>;
  return (
    <DataTable caption="Изменения" columns="minmax(0,1.4fr) 9rem minmax(0,1.2fr) 10rem" headers={['Название', 'Состояние', 'Что делает', 'Когда']}
      onRowSelect={(i) => onOpen(paged.rows[i].uid)} footer={paged.pager}
      rows={paged.rows.map((cs) => [
        <span key="t" className="flex flex-col"><span className="text-ui font-medium text-text">{cs.title}</span>{cs.last_error && <span className="text-meta text-danger-text">последняя ошибка: {cs.last_error}</span>}</span>,
        <StatusText key="s" label={cs.state === 'draft' ? 'черновик' : 'применён'} tone={cs.state === 'draft' ? 'warn' : 'ok'} />,
        <span key="k" className="text-meta">{kindCounts(cs)}</span>,
        <span key="d" className="text-meta text-text-muted">{cs.applied_at ? `применён ${dateTime(cs.applied_at)}` : `создан ${dateTime(cs.created_at)}`}</span>,
      ])} />
  );
}

type VTab = 'ops' | 'source' | 'json';

function ChangeView({ projectKey, uid, onBack }: { projectKey: string; uid: string; onBack: () => void }) {
  const [version, setVersion] = useState(0);
  const review = useAsync(`change-review:${projectKey}:${uid}:${version}`, () => pthApi.changesetReview(projectKey, uid));
  const [tab, setTab] = useState<VTab>('ops');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const act = async (name: string, fn: () => Promise<unknown>, success: string, leave = false) => {
    setBusy(name); setError(null); setNote(null);
    try { await fn(); if (leave) { onBack(); return; } setNote(success); setVersion((v) => v + 1); }
    catch (e) { setError(errText(e, 'Не получилось')); setVersion((v) => v + 1); }
    finally { setBusy(null); }
  };
  return (
    <div className="flex flex-col gap-4">
      <div><Button variant="secondary" size="sm" onClick={onBack}>Назад к изменениям</Button></div>
      <Loaded state={review} retry={review.reload}>
        {(r) => {
          const cs = r.changeset;
          const draft = cs.state === 'draft';
          return (
            <>
              <div className="flex flex-col gap-2 rounded-card border border-border bg-surface p-4">
                <div className="flex flex-wrap items-center gap-3">
                  <h2 className="text-section font-semibold text-text">{cs.title}</h2>
                  <StatusText label={draft ? 'черновик' : 'применён'} tone={draft ? 'warn' : 'ok'} />
                </div>
                <p className="text-ui text-text-muted">
                  {Object.entries(r.counts).filter(([k]) => COUNT_LABEL[k]).map(([k, n]) => plural(n, COUNT_LABEL[k])).join(' · ')} · создан {dateTime(cs.created_at)}{cs.applied_at ? ` · применён ${dateTime(cs.applied_at)}` : ''}
                </p>
                {draft ? (
                  <p className="text-ui text-text">{r.valid ? 'Проверка пройдена: все операции применились бы без ошибок. В проекте пока ничего не изменено.' : 'Применить сейчас нельзя: есть ошибка. Исправьте операции или отклоните черновик.'}</p>
                ) : <p className="text-ui text-text-muted">Применён: изменения уже в проекте, ChangeSet стал частью истории.</p>}
                {draft && r.first_error && (
                  <InlineAlert tone="danger" message={`Операция ${r.first_error.index + 1} (${KIND_LABEL[r.first_error.kind ?? ''] ?? r.first_error.kind}) не пройдёт: ${r.first_error.error}`} />
                )}
                {cs.last_error && draft && <InlineAlert tone="warning" message={`Прошлая попытка применения закончилась ошибкой: ${cs.last_error}`} />}
                {error && <InlineAlert tone="danger" message={error} />}
                {note && <InlineAlert tone="success" message={note} />}
                {draft && (
                  <div className="flex flex-wrap gap-2">
                    <Button variant="primary" disabled={!r.valid || busy !== null} loading={busy === 'apply'}
                      onClick={() => void act('apply', () => pthApi.applyChangeset(projectKey, uid), 'Применено: изменения записаны в проект.')}>Применить</Button>
                    <Button variant="secondary" disabled={busy !== null} loading={busy === 'check'} onClick={() => void act('check', async () => undefined, 'Проверено заново.')}>Проверить заново</Button>
                    <Button variant="secondary" disabled={busy !== null} loading={busy === 'discard'}
                      onClick={() => { if (window.confirm('Отклонить черновик? Он будет удалён без применения.')) void act('discard', () => pthApi.discardChangeset(projectKey, uid), '', true); }}>Отклонить</Button>
                  </div>
                )}
              </div>
              <Tabs label="Разделы изменения" idPrefix="change" selectedId={tab} onChange={(id) => setTab(id as VTab)}
                items={[{ id: 'ops', label: `Операции (${r.operations.length})` }, ...(r.source ? [{ id: 'source', label: 'Источник' }] : []), { id: 'json', label: 'JSON' }]} />
              <div role="tabpanel" id={`change-panel-${tab}`} aria-labelledby={`change-tab-${tab}`}>
                {tab === 'ops' && <Operations ops={r.operations} checked={r.checked} />}
                {tab === 'source' && r.source && <SourceTab review={r} />}
                {tab === 'json' && <JsonTab cs={cs} onSaved={() => setVersion((v) => v + 1)} projectKey={projectKey} />}
              </div>
            </>
          );
        }}
      </Loaded>
    </div>
  );
}

function Operations({ ops, checked }: { ops: ChangeOp[]; checked: boolean }) {
  const paged = usePaged(ops, 20, 'Страницы операций');
  return (
    <div className="flex flex-col gap-3">
      <ol className="flex flex-col gap-2" aria-label="Операции изменения">
        {paged.rows.map((op) => <OpCard key={op.index} op={op} checked={checked} />)}
      </ol>
      {paged.pager}
    </div>
  );
}

function OpCard({ op, checked }: { op: ChangeOp; checked: boolean }) {
  const status = op.status === 'ok' ? { label: 'пройдёт', tone: 'ok' as const } : op.status === 'error' ? { label: 'ошибка', tone: 'bad' as const } : op.status === 'skipped' ? { label: 'не проверялась', tone: 'neutral' as const } : null;
  return (
    <li className={`flex flex-col gap-2 rounded-card border p-3 ${op.status === 'error' ? 'border-danger-text' : 'border-border'} bg-surface`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-meta text-text-muted">{op.index + 1}.</span>
        <Badge label={KIND_LABEL[op.kind ?? ''] ?? String(op.kind)} tone={op.kind === 'create' ? 'success' : op.kind === 'revise' ? 'warning' : op.kind === 'unlink' || op.kind === 'archive' ? 'danger' : 'info'} />
        {op.display_id && <CopyId value={op.display_id} className="font-mono text-meta">{op.display_id}</CopyId>}
        {op.type && op.kind === 'create' && <span className="text-meta text-text-muted">{op.type}</span>}
        {checked && status && <span className="ml-auto"><StatusText label={status.label} tone={status.tone} /></span>}
      </div>
      {(op.kind === 'create' || op.kind === 'revise') && <ElementBody op={op} checked={checked} />}
      {(op.kind === 'link' || op.kind === 'unlink') && (
        <p className="text-ui text-text">
          <span className="font-mono text-meta">{op.source}</span> <span className="text-text-muted">{op.source_title ? `(${op.source_title}) ` : ''}</span>
          <strong>{op.kind === 'unlink' ? `больше не ${REL_LABEL[op.type ?? ''] ?? op.type}` : REL_LABEL[op.type ?? ''] ?? op.type}</strong>{' '}
          <span className="font-mono text-meta">{op.target}</span> <span className="text-text-muted">{op.target_title ? `(${op.target_title})` : ''}</span>
        </p>
      )}
      {(op.kind === 'archive' || op.kind === 'restore') && (
        <p className="text-ui text-text">
          {op.title && <span>{op.title}</span>}
          {op.kind === 'archive' && op.reason && <span className="text-text-muted"> — причина: {op.reason}</span>}
          {op.kind === 'archive' && op.replaced_by && <span className="text-text-muted"> · заменён на <span className="font-mono text-meta">{op.replaced_by}</span></span>}
        </p>
      )}
      {!['create', 'revise', 'link', 'unlink', 'archive', 'restore'].includes(op.kind ?? '') && <p className="text-ui text-danger-text">Неизвестная операция: {String(op.kind)}</p>}
      {op.error && <p className="text-meta text-danger-text">{op.error}</p>}
    </li>
  );
}

function ElementBody({ op, checked }: { op: ChangeOp; checked: boolean }) {
  const before = op.before;
  if (op.kind === 'revise' && !before) return <p className="text-ui text-danger-text">Элемента {op.display_id} нет в проекте: ревизию добавить некуда.</p>;
  return (
    <div className="flex flex-col gap-1.5">
      {before ? (
        <>
          <p className="text-meta text-text-muted">{checked
            ? `Появится ревизия ${before.revision + 1} вместо ${before.revision}${op.expected_revision != null && op.expected_revision !== before.revision ? `. Операция ждёт ревизию ${op.expected_revision}, а в проекте ${before.revision}: будет конфликт` : ''}`
            : `Была ревизия ${before.revision}, стала ${before.revision + 1}`}</p>
          {before.title !== op.title ? <div><span className="text-meta text-text-muted">Название</span><Diff before={before.title} after={op.title ?? ''} /></div> : <p className="text-ui text-text">{op.title}</p>}
          {before.text !== (op.text ?? '') && <div><span className="text-meta text-text-muted">Текст</span><Diff before={before.text} after={op.text ?? ''} /></div>}
          {before.priority !== (op.priority ?? null) && <p className="text-meta">Приоритет: <del className="text-danger-text">{before.priority ?? '—'}</del> → <ins className="text-success-text">{op.priority ?? '—'}</ins></p>}
          {before.title === op.title && before.text === (op.text ?? '') && before.priority === (op.priority ?? null) && <p className="text-meta text-text-muted">Содержимое не отличается от текущего.</p>}
        </>
      ) : (
        <>
          <p className="text-ui font-medium text-text">{op.title}</p>
          {op.text && <p className="whitespace-pre-wrap text-ui text-text-muted">{op.text}</p>}
          {op.priority && <p className="text-meta text-text-muted">Приоритет {op.priority}</p>}
          {op.external_key && <p className="text-meta text-text-muted">Внешний ID документа: <span className="font-mono">{op.external_key}</span></p>}
          {op.provenance?.quote && <p className="text-meta text-text-muted">Цитата модели{op.provenance.quote_verified === false ? ' (дословно не подтверждена)' : ''}: «{op.provenance.quote}»</p>}
        </>
      )}
    </div>
  );
}

function SourceTab({ review }: { review: ChangeSetReview }) {
  const s = review.source;
  const [active, setActive] = useState<string | null>(null);
  if (!s) return null;
  if (!s.text) return <p className="text-ui text-text-muted">Источник «{s.name}» сохранён вместе с применением; смотрите его на вкладке «Источник».</p>;
  const marks = s.fragments.map((f) => ({ ...f, display_id: f.display_id, quote: f.quote ?? null, verified: f.verified ?? null, start_col: f.start_col ?? null, end_col: f.end_col ?? null }));
  return (
    <div className="flex flex-col gap-2">
      <p className="text-meta text-text-muted">{s.name} · {s.lines} строк · подсвечены фрагменты, из которых получены элементы этого изменения</p>
      <SourcePane text={s.text} marks={marks} active={active} onSelect={setActive} />
    </div>
  );
}

function JsonTab({ cs, projectKey, onSaved }: { cs: ChangeSet; projectKey: string; onSaved: () => void }) {
  const [text, setText] = useState(JSON.stringify(cs.operations, null, 2));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const draft = cs.state === 'draft';
  const save = async () => {
    setError(null);
    let ops: unknown;
    try { ops = JSON.parse(text); } catch { setError('Это не JSON. Проверьте скобки и кавычки.'); return; }
    if (!Array.isArray(ops) || ops.length === 0 || !ops.every((o) => o && typeof o === 'object' && !Array.isArray(o))) { setError('Ожидается непустой список операций-объектов.'); return; }
    setSaving(true);
    try { await pthApi.editChangeset(projectKey, cs.uid, { operations: ops as Record<string, unknown>[] }); onSaved(); }
    catch (e) { setError(errText(e, 'Не удалось сохранить')); } finally { setSaving(false); }
  };
  return (
    <div className="flex flex-col gap-2">
      <p className="text-meta text-text-muted">{draft ? 'Для опытных: операции как они хранятся. После сохранения черновик проверяется заново. Номер ожидаемой ревизии сервер проставляет сам.' : 'Операции применённого изменения только для чтения.'}</p>
      <TextareaField label="Операции (JSON)" value={text} onChange={(e) => setText(e.target.value)} rows={16} readOnly={!draft} className="font-mono" />
      {error && <InlineAlert tone="danger" message={error} />}
      {draft && <div><Button variant="secondary" loading={saving} onClick={() => void save()}>Сохранить операции</Button></div>}
    </div>
  );
}
