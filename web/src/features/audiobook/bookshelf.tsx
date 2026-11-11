import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { HStack, Layout, LayoutContent, LayoutHeader, VStack } from '@astryxdesign/core/Layout';
import { ProgressBar } from '@astryxdesign/core/ProgressBar';
import { Heading, Text } from '@astryxdesign/core/Text';
import { useTranslator } from '@astryxdesign/core/i18n';
import { call } from '../../shared/lib/client';
import { formatCallError } from '../../shared/i18n/format';
import type { Book, Draft, Generation, QueueItem, Series } from '../../shared/workbench';
import { confirmChapterRead, verifyChapter } from './chapter-gate';
import {
  actionLabels, buildShelf, gateLabels, gateStatus, qaVerdict, qaVerdictLabels, queueTasks, shelfTasks,
  stageLabels, taskTarget, type ShelfAction, type ShelfChapter, type ShelfTask,
} from './shelf-model';

export interface BookshelfProps {
  books?: Book[];
  series?: Series[];
  /** 全部作品；书架只取其中的章节（故事类）。 */
  drafts: Draft[];
  history: Generation[];
  /** 打开某一章：该读与该校验去归因阅读页，渲染与验收去章节工作区。 */
  onOpen: (draft: Draft, target: 'reading' | 'create') => void;
  /** 书架是否可见；不可见时不重复请求服务端待办。 */
  isActive: boolean;
  /** 闸门动作成功后取回这一章的最新状态。 */
  onRefreshChapter?: (id: string) => Promise<void>;
}

type QueueSource = 'loading' | 'server' | 'local';

const queueSourceLabels: Record<QueueSource, string> = {
  loading: '正在读取服务端待办…',
  server: '待办来自服务端队列',
  local: '服务端待办不可用，按本地状态推导',
};

// 书架（ADR-0012）：按系列与卷归组章节，显示每章闸门、验收与派生进度，并把未完成动作排成一条队列。
export function Bookshelf({ books, series, drafts, history, onOpen, isActive, onRefreshChapter }: BookshelfProps) {
  const t = useTranslator();
  const groups = useMemo(() => buildShelf(books, series, drafts, history), [books, series, drafts, history]);
  // 空的系列与卷不画空框：数据层保留完整归组，渲染前只留下有章节的卷。
  const visibleGroups = useMemo(() => groups.flatMap(group => {
    const items = group.books.filter(item => item.chapters.length > 0);
    return items.length ? [{ ...group, books: items }] : [];
  }), [groups]);
  const localTasks = shelfTasks(visibleGroups);
  const [queued, setQueued] = useState<ShelfTask[] | null>(null);
  const [source, setSource] = useState<QueueSource>('loading');
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState('');
  const [actionError, setActionError] = useState('');
  const [activeChapter, setActiveChapter] = useState<string | null>(null);
  const contentRef = useRef<HTMLElement>(null);
  const tasks = queued ?? localTasks;
  const visibleTasks = tasks.slice(0, 3);
  const chapterIds = visibleGroups.flatMap(group => group.books.flatMap(item => item.chapters.map(chapter => chapter.draft.id)));
  const activeId = activeChapter && chapterIds.includes(activeChapter) ? activeChapter : undefined;
  const tabbableId = activeId ?? chapterIds[0];
  // 状态推送里的活动进度会换掉作品数组，但章节进度没变时不必重取待办；用内容签名决定何时请求。
  const queueKey = visibleGroups.map(group => group.books.map(item => item.chapters
    .map(chapter => `${chapter.draft.id}:${chapter.stage}:${gateStatus(chapter.draft)}:${qaVerdict(chapter.draft)}`).join(',')).join(';')).join('|');
  const latestGroups = useRef(visibleGroups);
  latestGroups.current = visibleGroups;

  // 待办优先取服务端队列；任一卷取不到（旧服务还没有 queue.pending）时回退到本地派生，不拼假数据。
  useEffect(() => {
    const groups = latestGroups.current;
    if (!groups.length) {
      setQueued(null);
      setSource('local');
      return;
    }
    if (!isActive) return;
    let cancelled = false;
    setQueued(null);
    setSource('loading');
    void Promise.all(groups.flatMap(group => group.books.map(async item => ({
      bookId: item.book.id,
      items: await call<QueueItem[] | null>('queue.pending', { bookId: item.book.id }),
    })))).then(queues => {
      if (cancelled) return;
      setQueued(queueTasks(groups, queues.flatMap(queue => queue.items ?? [])));
      setSource('server');
    }).catch(() => {
      if (cancelled) return;
      setQueued(null);
      setSource('local');
    });
    return () => { cancelled = true; };
  }, [isActive, queueKey, revision]);

  function openChapter(chapter: ShelfChapter) {
    onOpen(chapter.draft, chapter.draft.subtitles ? 'reading' : 'create');
  }

  // 上下键在整张书架的章节行之间移动焦点；回车交给按钮自身的点击行为打开这一章。
  function focusChapter(event: KeyboardEvent<HTMLButtonElement>) {
    const step = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const rows = Array.from(contentRef.current?.querySelectorAll<HTMLButtonElement>('.audiobook-chapter-open') ?? []);
    const current = rows.indexOf(event.currentTarget);
    const next = rows[Math.min(Math.max(current + step, 0), rows.length - 1)];
    next?.focus();
  }

  // 确认已读与确认校验可以直接在书架上完成；渲染与验收入口仍然跳到章节工作区。
  async function confirmTask(task: ShelfTask) {
    if (task.action !== 'read' && task.action !== 'verify') return;
    const key = `${task.chapter.draft.id}:${task.action}`;
    setBusy(key);
    setActionError('');
    try {
      if (task.action === 'read') await confirmChapterRead(task.chapter.draft.id);
      else await verifyChapter(task.chapter.draft.id);
      await onRefreshChapter?.(task.chapter.draft.id).catch(() => undefined);
      setRevision(value => value + 1);
    } catch (cause) {
      setActionError(formatCallError(t, cause));
    } finally {
      setBusy('');
    }
  }

  const confirmable = (action: ShelfAction) => action === 'read' || action === 'verify';

  return <Layout className="audiobook-shelf" height="fill" padding={0}
    header={<LayoutHeader hasDivider padding={4}>
      <VStack className="audiobook-shelf-header" gap={2}>
        <Heading level={1}>书架</Heading>
        <Text type="supporting">按系列归组。进度由章节的闸门、生成版本与验收记录派生，不另存状态；选中章节后按回车打开。</Text>
      </VStack>
    </LayoutHeader>}
    content={<LayoutContent padding={0} label="书架" role="region">
      <VStack ref={contentRef} className="audiobook-shelf-content" gap={5} data-testid="audiobook-shelf">
        {visibleGroups.length ? <>
          <VStack className="audiobook-next" gap={2} data-testid="audiobook-next">
            <HStack hAlign="between" vAlign="start" gap={3} wrap="wrap">
              <VStack gap={0}>
                <Heading level={2}>下一件事</Heading>
                <Text type="supporting">按章节顺序排列，一次显示最前面的 3 件{tasks.length > 3 ? `（还有 ${tasks.length - 3} 件）` : ''}</Text>
              </VStack>
              <Text className="audiobook-queue-source" type="supporting" data-testid="audiobook-queue-source" data-source={source}>{queueSourceLabels[source]}</Text>
            </HStack>
            {visibleTasks.length ? <VStack as="ol" className="audiobook-next-list" gap={0}>
              {visibleTasks.map((task, index) => {
                const key = `${task.chapter.draft.id}:${task.action}`;
                return <HStack as="li" key={key} className="audiobook-next-item" gap={3} vAlign="center" data-testid="audiobook-next-item" data-action={task.action}>
                  <Text className="audiobook-next-index" type="code" hasTabularNumbers>{index + 1}</Text>
                  <VStack className="audiobook-next-body" gap={0}>
                    <Text className="audiobook-next-title" type="label">{task.chapter.draft.title}</Text>
                    <Text type="supporting">{task.reason || `${task.book.title} · 第 ${task.chapter.number} 章 · 现在：${stageLabels[task.chapter.stage]}`}</Text>
                  </VStack>
                  <HStack className="audiobook-next-actions" gap={1} vAlign="center" wrap="wrap">
                    {confirmable(task.action) ? <Button size="sm" variant="secondary" data-testid="audiobook-next-confirm"
                      label={task.action === 'read' ? '确认已读' : '确认校验'} isLoading={busy === key} isDisabled={!!busy}
                      onClick={() => void confirmTask(task)} /> : null}
                    <Button className="audiobook-next-action" size="sm" variant="primary" label={actionLabels[task.action]}
                      data-testid="audiobook-next-action" data-action={task.action} isDisabled={!!busy}
                      onClick={() => onOpen(task.chapter.draft, taskTarget(task))} />
                  </HStack>
                </HStack>;
              })}
            </VStack> : <Text type="supporting">所有章节都已完成验收。</Text>}
            {actionError ? <Text className="audiobook-action-error" type="supporting" role="alert" data-testid="audiobook-next-error">{actionError}</Text> : null}
          </VStack>

          {visibleGroups.map(group => <VStack key={group.id || 'standalone'} className="audiobook-shelf-series" gap={3} data-testid="audiobook-shelf-series">
            <Heading level={2} className="audiobook-series-title">{group.title}</Heading>
            {group.books.map(item => <VStack key={item.book.id} className="audiobook-book" gap={2} data-testid="audiobook-book" data-book-id={item.book.id}>
              <HStack className="audiobook-book-head" gap={3} vAlign="center" wrap="wrap">
                <Heading level={3} className="audiobook-book-title">{item.book.title}</Heading>
                <Text type="supporting">{item.chapters.length} 章</Text>
                <HStack className="audiobook-book-progress" gap={2} vAlign="center" wrap="wrap" data-testid="audiobook-progress">
                  <ProgressBar className="audiobook-progress-bar" label={`${item.book.title} 已验收进度`} value={item.done} max={item.chapters.length}
                    isLabelHidden variant={item.done === item.chapters.length ? 'success' : 'accent'} data-testid="audiobook-progress-bar" />
                  <Text type="label">已验收 {item.done}/{item.chapters.length}</Text>
                  {(['imported', 'read', 'attributed', 'verified', 'rendered'] as const).filter(stage => item.counts[stage] > 0).map(stage =>
                    <Text key={stage} type="supporting">{stageLabels[stage]} {item.counts[stage]}</Text>)}
                </HStack>
              </HStack>
              <VStack as="ol" className="audiobook-chapter-rows" gap={0}>
                {item.chapters.map(chapter => <HStack as="li" key={chapter.draft.id} className="audiobook-chapter-row"
                  data-testid="audiobook-chapter" data-chapter-id={chapter.draft.id} data-stage={chapter.stage}
                  data-active={activeId === chapter.draft.id ? 'true' : undefined}>
                  <button type="button" className="audiobook-chapter-open" data-chapter-id={chapter.draft.id}
                    tabIndex={tabbableId === chapter.draft.id ? 0 : -1}
                    aria-label={`第 ${chapter.number} 章，${chapter.draft.title}，${stageLabels[chapter.stage]}`}
                    title={chapter.draft.title} onFocus={() => setActiveChapter(chapter.draft.id)}
                    onKeyDown={focusChapter} onClick={() => openChapter(chapter)}>
                    <Text as="span" className="audiobook-chapter-no" type="code" hasTabularNumbers>{chapter.number}</Text>
                    <VStack as="span" className="audiobook-chapter-title" gap={0}>
                      <Text as="span" className="audiobook-chapter-title-text" type="label">{chapter.draft.title}</Text>
                      <Text as="span" className="audiobook-chapter-stage" type="code" data-stage={chapter.stage}>{stageLabels[chapter.stage]}</Text>
                    </VStack>
                    <Text as="span" className="audiobook-chapter-meta" type="supporting" data-testid="audiobook-chapter-gate">闸门：{gateLabels[gateStatus(chapter.draft)]}</Text>
                    <Text as="span" className="audiobook-chapter-meta" type="supporting" data-testid="audiobook-chapter-qa">验收：{qaVerdictLabels[qaVerdict(chapter.draft)]}</Text>
                  </button>
                </HStack>)}
              </VStack>
            </VStack>)}
          </VStack>)}
          <Text type="supporting">混音产物的记录还不在状态里，因此这里不显示“已混音”一列。</Text>
        </> : <EmptyState title="还没有书" description="导入整本书后，章节会按系列与卷归在这里，并显示逐章进度。" />}
      </VStack>
    </LayoutContent>} />;
}
