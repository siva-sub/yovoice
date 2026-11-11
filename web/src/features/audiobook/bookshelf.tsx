import { Button } from '@astryxdesign/core/Button';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { HStack, Layout, LayoutContent, LayoutHeader, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import type { Book, Draft, Generation, Series } from '../../shared/workbench';
import { actionLabels, buildShelf, gateLabels, gateStatus, qaVerdict, qaVerdictLabels, shelfTasks, stageLabels, taskTarget } from './shelf-model';

export interface BookshelfProps {
  books?: Book[];
  series?: Series[];
  /** 全部作品；书架只取其中的章节（故事类）。 */
  drafts: Draft[];
  history: Generation[];
  /** 打开某一章：该读与该校验去归因阅读页，渲染与验收去章节工作区。 */
  onOpen: (draft: Draft, target: 'reading' | 'create') => void;
}

// 书架（ADR-0012）：按系列与卷归组章节，显示每章闸门、验收与派生进度，并把未完成动作排成一条队列。
export function Bookshelf({ books, series, drafts, history, onOpen }: BookshelfProps) {
  const groups = buildShelf(books, series, drafts, history);
  const tasks = shelfTasks(groups);
  const visibleTasks = tasks.slice(0, 3);
  return <Layout className="audiobook-shelf" height="fill" padding={0}
    header={<LayoutHeader hasDivider padding={4}>
      <VStack className="audiobook-shelf-header" gap={2}>
        <Heading level={1}>书架</Heading>
        <Text type="supporting">按系列归组。进度由章节的闸门、生成版本与验收记录派生，不另存状态。</Text>
      </VStack>
    </LayoutHeader>}
    content={<LayoutContent padding={0} label="书架" role="region">
      <VStack className="audiobook-shelf-content" gap={5} data-testid="audiobook-shelf">
        {groups.length ? <>
          <VStack className="audiobook-next" gap={2} data-testid="audiobook-next">
            <HStack hAlign="between" vAlign="center" gap={3} wrap="wrap">
              <Heading level={2}>下一件事</Heading>
              <Text type="supporting">按章节顺序排列，一次显示最前面的 3 件{tasks.length > 3 ? `（还有 ${tasks.length - 3} 件）` : ''}</Text>
            </HStack>
            {visibleTasks.length ? <VStack as="ol" className="audiobook-next-list" gap={0}>
              {visibleTasks.map((task, index) => <HStack as="li" key={task.chapter.draft.id} className="audiobook-next-item" gap={3} vAlign="center" data-testid="audiobook-next-item" data-action={task.action}>
                <Text className="audiobook-next-index" type="code" hasTabularNumbers>{index + 1}</Text>
                <VStack className="audiobook-next-body" gap={0}>
                  <Text type="label">{task.chapter.draft.title}</Text>
                  <Text type="supporting">{task.book.title} · 第 {task.chapter.number} 章 · 现在：{stageLabels[task.chapter.stage]}</Text>
                </VStack>
                <Button className="audiobook-next-action" size="sm" variant="primary" label={actionLabels[task.action]} data-testid="audiobook-next-action" data-action={task.action} onClick={() => onOpen(task.chapter.draft, taskTarget(task))} />
              </HStack>)}
            </VStack> : <Text type="supporting">所有章节都已完成验收。</Text>}
          </VStack>

          {groups.map(group => <VStack key={group.id || 'standalone'} className="audiobook-shelf-series" gap={3} data-testid="audiobook-shelf-series">
            <Heading level={2}>{group.title}</Heading>
            {group.books.map(item => <VStack key={item.book.id} className="audiobook-book" gap={2} data-testid="audiobook-book" data-book-id={item.book.id}>
              <HStack className="audiobook-book-head" gap={3} vAlign="center" wrap="wrap">
                <Heading level={3}>{item.book.title}</Heading>
                <Text type="supporting">{item.chapters.length} 章</Text>
                <HStack className="audiobook-book-progress" gap={3} vAlign="center" wrap="wrap" data-testid="audiobook-progress">
                  <Text type="label">已验收 {item.done}/{item.chapters.length}</Text>
                  {(['imported', 'read', 'attributed', 'verified', 'rendered'] as const).filter(stage => item.counts[stage] > 0).map(stage =>
                    <Text key={stage} type="supporting">{stageLabels[stage]} {item.counts[stage]}</Text>)}
                </HStack>
              </HStack>
              {item.chapters.length ? <VStack as="ol" className="audiobook-chapter-rows" gap={0}>
                {item.chapters.map(chapter => <HStack as="li" key={chapter.draft.id} className="audiobook-chapter-row" gap={3} vAlign="center" data-testid="audiobook-chapter" data-chapter-id={chapter.draft.id} data-stage={chapter.stage}>
                  <Text className="audiobook-chapter-no" type="code" hasTabularNumbers>{chapter.number}</Text>
                  <VStack className="audiobook-chapter-title" gap={0}>
                    <Text type="label">{chapter.draft.title}</Text>
                    <Text className="audiobook-chapter-stage" type="code" data-stage={chapter.stage}>{stageLabels[chapter.stage]}</Text>
                  </VStack>
                  <Text className="audiobook-chapter-meta" type="supporting" data-testid="audiobook-chapter-gate">闸门：{gateLabels[gateStatus(chapter.draft)]}</Text>
                  <Text className="audiobook-chapter-meta" type="supporting" data-testid="audiobook-chapter-qa">验收：{qaVerdictLabels[qaVerdict(chapter.draft)]}</Text>
                </HStack>)}
              </VStack> : <Text type="supporting">这本书还没有章节。</Text>}
            </VStack>)}
          </VStack>)}
          <Text type="supporting">混音产物的记录还不在状态里，因此这里不显示“已混音”一列。</Text>
        </> : <EmptyState title="还没有书" description="导入整本书后，章节会按系列与卷归在这里，并显示逐章进度。" />}
      </VStack>
    </LayoutContent>} />;
}
