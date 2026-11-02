import { Fragment } from 'react';
import { pthApi } from '@/shared/api/pthApi';
import { useAsync } from '@/shared/lib/useAsync';
import { ArtifactEditor } from './ArtifactEditor';
import { useState } from 'react';
import { Badge, Button, InlineAlert } from '@/shared/ui';
import type { ComplianceItem, RelationRow } from '@/entities/pth/types';
import { Loaded, Section, StatusText } from './common';
import { RevisionHistory } from './RevisionHistory';
import { DiagramView, formatOf } from '@/features/diagrams/DiagramView';
import { useOpenSource } from '@/shared/lib/sourceNav';
import { CopyId } from '@/shared/ui/CopyId';

function RelationEnd({ id, onOpen }: { id: string; onOpen?: (displayId: string) => void }) {
  if (!onOpen) return <CopyId value={id}>{id}</CopyId>;
  return (
    <CopyId value={id}>
      <button type="button" className="text-accent-text underline underline-offset-2 hover:no-underline" onClick={() => onOpen(id)}>
        {id}
      </button>
    </CopyId>
  );
}

/** Карточка элемента: текст, атрибуты, связи в проекте и история ревизий. Используется в реестре и во влиянии. */
export function ArtifactCard({ projectKey, displayId, onOpen, hideDiagram = false }: { projectKey: string; displayId: string; /** Не рисовать схему: она уже показана рядом. */ hideDiagram?: boolean; /** Переход к другому элементу по ссылке из связи. Без него ID — текст. */ onOpen?: (displayId: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [version, setVersion] = useState(0);
  const detail = useAsync(`card:${projectKey}:${displayId}:${version}`, () => pthApi.artifact(projectKey, displayId));
  const revisions = useAsync(`revs:${projectKey}:${displayId}:${version}`, () => pthApi.revisions(projectKey, displayId));
  const relations = useAsync(`rels:${projectKey}:${displayId}`, () => pthApi.relations(projectKey));
  const compliance = useAsync(`compl:${projectKey}:${displayId}:${version}`, () => pthApi.compliance(projectKey));
  return (
    <Section title={`Карточка ${displayId}`}>
      <Loaded state={detail} retry={detail.reload}>
        {(d) => editing ? (
          <ArtifactEditor projectKey={projectKey} card={d}
            onSaved={() => { setEditing(false); setVersion((v) => v + 1); }}
            onCancel={() => setEditing(false)} />
        ) : (
          <div className="flex flex-col gap-4">
            {/* Архивный элемент только для чтения (409 ARTIFACT_ARCHIVED); вернуть — через restore. */}
            {d.archived_at && (
              <InlineAlert tone="warning"
                message={`В архиве с ${new Date(d.archived_at).toLocaleDateString('ru-RU')}: ${d.archive_reason ?? 'причина не указана'}. Элемент не участвует в анализе, его связи скрыты, правка недоступна.`}
                action={d.replaced_by ? <span className="text-meta">Преемник: <RelationEnd id={d.replaced_by} onOpen={onOpen} /></span> : undefined} />
            )}
            <div className="flex flex-col gap-2 rounded-card border border-border bg-surface p-4">
              <h3 className="text-ui font-semibold">{d.title}</h3>
              {d.type === 'DIAGRAM' && !hideDiagram && <DiagramView format={formatOf(d.attributes)} source={d.text} title={d.title} />}
              <div className="flex items-start justify-between gap-2">
                {d.type === 'DIAGRAM'
                  ? <p className="text-ui text-text-muted">Исходник схемы не показывается в карточке. Редактируется на вкладке «Редактирование».</p>
                  : <p className="whitespace-pre-wrap text-ui text-text">{d.text || 'Текст не задан.'}</p>}
                {d.type !== 'DIAGRAM' && !d.archived_at && <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>Редактировать</Button>}
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-meta">
                <dt className="text-text-muted">тип</dt><dd>{d.type}</dd>
                <dt className="text-text-muted">ревизия</dt><dd>{d.revision}</dd>
                {/* Внешний ключ не дублируем: у коммита он совпадает с атрибутом sha. */}
                {d.external_key && !Object.values(d.attributes).includes(d.external_key) && (
                  <><dt className="text-text-muted">внешний ключ</dt><dd className="font-mono"><CopyId value={d.external_key}>{d.external_key}</CopyId></dd></>
                )}
                {Object.entries(d.attributes).map(([k, v]) => (
                  <Fragment key={k}><dt className="text-text-muted">{k}</dt><dd>{String(v)}</dd></Fragment>
                ))}
              </dl>
              {d.exemption_reason && <p className="text-meta text-text-muted">Исключение: {d.exemption_reason}</p>}
            </div>
            {(d.type === 'US' || d.type === 'FR') && <ProcessDiagrams projectKey={projectKey} displayId={displayId} relations={relations} onOpen={onOpen} />}
            <Origins projectKey={projectKey} displayId={displayId} />
            {!d.archived_at && <ImplementationStatus displayId={displayId} type={d.type} excluded={Boolean(d.exemption_reason)}
              relations={relations} compliance={compliance} />}
            <div className="flex flex-col gap-3 rounded-card border border-border bg-surface p-4">
              <h3 className="text-ui font-semibold">Связи</h3>
              <Loaded state={relations} retry={relations.reload}>
                {(all) => {
                  const mine = all.filter((r) => r.source === displayId || r.target === displayId);
                  return mine.length === 0 ? <p className="text-ui text-text-muted">Связей нет.</p> : (
                    <ul className="flex flex-col gap-1 font-mono text-meta">
                      {mine.map((r) => (
                        <li key={r.uid} className="flex flex-wrap items-center gap-x-1">
                          <RelationEnd id={r.source} onOpen={onOpen} />
                          <span className="text-text-muted">—{r.type}→</span>
                          <RelationEnd id={r.target} onOpen={onOpen} />
                        </li>
                      ))}
                    </ul>
                  );
                }}
              </Loaded>
              <h3 className="mt-2 text-ui font-semibold">История ревизий</h3>
              <Loaded state={revisions} retry={revisions.reload}>
                {(revs) => <RevisionHistory revs={revs} textHidden={d.type === 'DIAGRAM'} />}
              </Loaded>
            </div>
          </div>
        )}
      </Loaded>
    </Section>
  );
}

const COMPLIANCE_LABEL: Record<ComplianceItem['status'], { text: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }> = {
  verified: { text: 'покрыт тестами, проверка пройдена', tone: 'ok' },
  partial: { text: 'покрыт тестами частично', tone: 'warn' },
  failed: { text: 'тест провален', tone: 'bad' },
  stale: { text: 'проверка устарела', tone: 'warn' },
  not_verified: { text: 'тест не выполнен или ожидает проверки', tone: 'warn' },
  no_test: { text: 'тестов нет', tone: 'neutral' },
  excluded: { text: 'исключён из проверки', tone: 'neutral' },
};
const TEST_LABEL: Record<string, string> = {
  verified: 'пройден', partial: 'пройден', failed: 'провален', stale: 'устарел', not_verified: 'не выполнен',
};
/** Требования, которые должны быть реализованы коммитом (связь implements). */
const NEEDS_CODE = new Set(['FR', 'NFR', 'AC']);

/** Реализация (коммиты по связи implements) и проверка (тесты по связи verifies) — что известно по данным PTH. */
function ImplementationStatus({ displayId, type, excluded, relations, compliance }: {
  displayId: string;
  type: string;
  excluded: boolean;
  relations: { data: RelationRow[] | null; error: string | null; loading: boolean; reload: () => void };
  compliance: { data: { items: ComplianceItem[] } | null; error: string | null; loading: boolean; reload: () => void };
}) {
  return (
    <div className="flex flex-col gap-2 rounded-card border border-border bg-surface p-4">
      <h3 className="text-ui font-semibold">Реализация и проверка</h3>
      <Loaded state={relations} retry={relations.reload}>
        {(all) => {
          const commits = all.filter((r) => r.type === 'implements' && r.target === displayId).map((r) => r.source);
          const implLabel = commits.length > 0
            ? `реализован коммитами: ${commits.join(', ')}`
            : NEEDS_CODE.has(type) ? 'не реализован: нет связи implements' : 'к коду не относится';
          const tone = commits.length > 0 ? 'ok' : NEEDS_CODE.has(type) ? 'warn' : 'neutral';
          return (
            <div className="flex flex-col gap-1.5 text-ui">
              <StatusText label={implLabel} tone={tone} />
              <Loaded state={compliance} retry={compliance.reload}>
                {(report) => {
                  const item = report.items.find((i) => i.display_id === displayId);
                  const status = excluded ? 'excluded' : item?.status ?? null;
                  if (!status) return <StatusText label="не проверяется тестами" tone="neutral" />;
                  const check = COMPLIANCE_LABEL[status];
                  return (
                    <>
                      <StatusText label={check.text} tone={check.tone} />
                      {item && item.tests.length > 0 && (
                        <ul className="flex flex-col gap-0.5 pl-3.5 font-mono text-meta text-text-muted">
                          {item.tests.map((t) => (
                            <li key={t.display_id}><CopyId value={t.display_id}>{t.display_id}</CopyId>: {TEST_LABEL[t.status] ?? t.status}</li>
                          ))}
                        </ul>
                      )}
                    </>
                  );
                }}
              </Loaded>
            </div>
          );
        }}
      </Loaded>
    </div>
  );
}

/** Схемы, привязанные к процессу (US или ФТ): превью каждой, открывается по клику на ID. */
function ProcessDiagrams({ projectKey, displayId, relations, onOpen }: { projectKey: string; displayId: string; relations: { data: RelationRow[] | null; error: string | null; loading: boolean; reload: () => void }; onOpen?: (id: string) => void }) {
  const ids = (relations.data ?? []).filter((r) => r.type === 'illustrates' && r.target === displayId).map((r) => r.source);
  if (ids.length === 0) return null;
  return (
    <div className="flex flex-col gap-3 rounded-card border border-border bg-surface p-4">
      <h3 className="text-ui font-semibold">Схемы процесса: {ids.length}</h3>
      {ids.map((id) => <ProcessDiagram key={id} projectKey={projectKey} id={id} onOpen={onOpen} />)}
    </div>
  );
}

function ProcessDiagram({ projectKey, id, onOpen }: { projectKey: string; id: string; onOpen?: (id: string) => void }) {
  const card = useAsync(`proc-diagram:${projectKey}:${id}`, () => pthApi.artifact(projectKey, id));
  return (
    <Loaded state={card} retry={card.reload}>
      {(c) => (
        <div className="flex flex-col gap-2">
          <p className="text-ui">
            {onOpen ? <button type="button" className="font-mono text-meta text-accent-text underline underline-offset-2" onClick={() => onOpen(id)}>{id}</button> : <span className="font-mono text-meta">{id}</span>} <span className="text-text-muted">{c.title}</span>
          </p>
          <DiagramView format={formatOf(c.attributes)} source={c.text} title={c.title} />
        </div>
      )}
    </Loaded>
  );
}

/** Первоисточник: из какой строки какого документа получено требование; ведёт в источник с подсветкой. */
function Origins({ projectKey, displayId }: { projectKey: string; displayId: string }) {
  const open = useOpenSource();
  const origins = useAsync(`origins:${projectKey}:${displayId}`, () => pthApi.origins(projectKey, displayId));
  return (
    <Loaded state={origins} retry={origins.reload}>
      {(list) => (
        <div className="flex flex-col gap-2 rounded-card border border-border bg-surface p-4">
          <h3 className="mb-1 text-ui font-semibold">Первоисточник{list.length > 1 ? `: ${list.length} версии` : ''}</h3>
          {list.length === 0 ? <p className="text-ui text-text-muted">Источника нет: элемент создан вручную, а не импортом.</p> : (
            <ol className="flex flex-col gap-3" aria-label="Источники требования">
              {list.map((o, i) => {
                const older = list[i + 1];
                const llm = o.via === 'llm';
                const change = !older ? null
                  : o.text === older.text && o.line === older.line ? 'без изменений относительно предыдущего'
                  : o.text === older.text ? `формулировка та же, строка сместилась: ${older.line} → ${o.line}`
                  : 'формулировка изменена относительно предыдущего';
                return (
                  <li key={`${o.import_uid}:${o.line}`} className={`flex flex-col gap-2 rounded-control border p-3 ${i === 0 ? 'border-accent-text/60' : 'border-border'}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      {list.length > 1 && <Badge label={`${i + 1} из ${list.length}`} />}
                      {list.length > 1 && <Badge label={i === 0 ? 'последняя версия' : 'более ранняя'} tone={i === 0 ? 'accent' : 'neutral'} />}
                      <Badge label={llm ? 'цитата модели' : 'строка таблицы'} tone={llm ? 'info' : 'neutral'} />
                      <span className="text-meta text-text-muted">
                        {o.source_name} · {o.end_line > o.line ? `строки ${o.line}–${o.end_line}` : `строка ${o.line}`} · импорт {new Date(o.imported_at).toLocaleString('ru-RU')}
                      </span>
                    </div>
                    <p className={`whitespace-pre-wrap break-words rounded-control border-l-4 px-3 py-1.5 font-mono text-meta text-text ${llm ? `border-dashed ${o.verified === false ? 'border-warning-text' : 'border-info-text'} bg-surface-muted` : 'border-accent-text/60 bg-accent-soft/60'}`}>{o.text}</p>
                    {llm && o.verified === false && <p className="text-meta text-warning-text">Цитата модели дословно в тексте не подтверждена: проверьте формулировку.</p>}
                    {change && <p className={`text-meta ${change.includes('изменена') ? 'text-warning-text' : 'text-text-muted'}`}>{change}</p>}
                    {open && <div className="pt-0.5"><Button size="sm" variant="secondary" onClick={() => open({ uid: o.import_uid, line: o.line, displayId })}>Открыть в источнике</Button></div>}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
    </Loaded>
  );
}
