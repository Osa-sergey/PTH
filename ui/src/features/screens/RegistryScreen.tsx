import { useState } from 'react';
import { pthApi, type ArchiveFilter } from '@/shared/api/pthApi';
import { useAsync } from '@/shared/lib/useAsync';
import { Button, Pager, TextField } from '@/shared/ui';
import { DataTable, Loaded, Section } from './common';
import { ArtifactCard } from './ArtifactCard';
import { CopyId } from '@/shared/ui/CopyId';

export function RegistryScreen({ projectKey, initialType = '' }: { projectKey: string; initialType?: string }) {
  const [query, setQuery] = useState('');
  const [type, setType] = useState(initialType);
  const [archived, setArchived] = useState<ArchiveFilter>('exclude');
  const [submitted, setSubmitted] = useState({ q: '', type: initialType, archived: 'exclude' as ArchiveFilter });
  const [selected, setSelected] = useState<string | null>(null);
  // Переход по связи открывает другую карточку с начала страницы; key сбрасывает режим редактирования.
  const openCard = (displayId: string) => {
    setSelected(displayId);
    window.scrollTo({ top: 0 });
  };
  const [page, setPage] = useState(0);
  const PAGE = 20;
  const list = useAsync(`registry:${projectKey}:${submitted.q}:${submitted.type}:${submitted.archived}:${page}`, async () => {
    // Поиск сервер ведёт только по действующим элементам, поэтому при запросе фильтр архива не применяется.
    if (submitted.q) {
      const found = await pthApi.search(projectKey, submitted.q, submitted.type || undefined);
      return { items: found.slice(0, PAGE), total: found.length };
    }
    return pthApi.artifactsPage(projectKey, page * PAGE, PAGE, submitted.type || undefined,
      submitted.archived === 'exclude' ? undefined : submitted.archived);
  });

  // Карточка открывается на весь экран; поиск, тип и страница сохраняются при возврате.
  if (selected) {
    return (
      <div className="flex flex-col gap-4">
        <div>
          <Button variant="secondary" size="sm" onClick={() => setSelected(null)}>Назад к списку</Button>
        </div>
        <ArtifactCard key={selected} projectKey={projectKey} displayId={selected} onOpen={openCard} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Section title="Реестр" hint="Поиск по ID, названию и тексту текущих ревизий. Выберите элемент, чтобы открыть карточку.">
        <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); setSubmitted({ q: query.trim(), type, archived }); setPage(0); }}>
          <TextField label="Поиск" value={query} onChange={(e) => setQuery(e.target.value)} className="w-72" />
          <label className="flex flex-col gap-1 text-meta text-text-muted">
            Тип
            <select value={type} onChange={(e) => setType(e.target.value)} className="h-9 rounded-control border border-control-border bg-surface px-2 text-ui text-text">
              <option value="">все</option>
              {['US', 'FR', 'NFR', 'AC', 'ADR', 'TEST', 'COMMIT', 'DIAGRAM'].map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-meta text-text-muted">
            Архив
            <select value={archived} onChange={(e) => setArchived(e.target.value as ArchiveFilter)} disabled={Boolean(query.trim())}
              title={query.trim() ? 'Поиск ищет только действующие элементы' : undefined}
              className="h-9 rounded-control border border-control-border bg-surface px-2 text-ui text-text disabled:opacity-60">
              <option value="exclude">без архива</option>
              <option value="only">только архив</option>
              <option value="include">все</option>
            </select>
          </label>
          <Button type="submit" variant="secondary">Найти</Button>
        </form>
        <Loaded state={list} retry={list.reload} empty={{ title: 'Ничего не найдено', description: 'Измените запрос или тип.', isEmpty: (d) => d.items.length === 0 }}>
          {({ items: rows, total }) => (
            <DataTable
              caption="Элементы проекта"
              columns="8rem 5rem minmax(0,1fr) 6rem 7rem 12rem"
              onRowSelect={(i) => setSelected(rows[i].display_id)}
              selectedIndex={rows.findIndex((r) => r.display_id === selected)}
              headers={['ID', 'Тип', 'Название', 'Ревизия', 'Исключение', 'Архив']}
              footer={
                submitted.q ? (
                  <span className="text-meta text-text-muted">Совпадений: {total}{total > rows.length ? `, показаны первые ${rows.length}` : ''}. Для полного списка очистите поиск.</span>
                ) : (
                  <Pager page={page} pageSize={PAGE} total={total} onPage={setPage} label="Страницы реестра" />
                )
              }
              rows={rows.map((r) => [
                <CopyId key="id" value={r.display_id}><button type="button" className="font-mono text-meta text-accent-text underline-offset-2 hover:underline" onClick={() => setSelected(r.display_id)} aria-pressed={selected === r.display_id}>{r.display_id}</button></CopyId>,
                <span key="t" className="text-meta font-semibold text-text-muted">{r.type}</span>,
                <span key="n" className="text-text">{r.title}</span>,
                String(r.revision),
                r.exemption_reason ? 'да' : '—',
                r.archived_at
                  ? <span key="a" className="text-meta text-text-muted" title={r.archive_reason ?? undefined}>в архиве{r.replaced_by ? ` → ${r.replaced_by}` : ''}</span>
                  : '—',
              ])}
            />
          )}
        </Loaded>
      </Section>

    </div>
  );
}
