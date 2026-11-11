import { projectKind, type Book, type Draft, type Gate, type Generation, type QARecord, type QueueItem, type Series } from '../../shared/workbench';

// 书架的派生逻辑：章节阶段、按系列归组与“下一件事”，全部只读状态，不新增后端字段。

/** 章节在书级流水线上的阶段，与 workbench.ChapterProgress 一致；mixed 没有状态字段，最远停在 qa。 */
export type ChapterStage = 'imported' | 'read' | 'attributed' | 'verified' | 'rendered' | 'qa';

export const stageLabels: Record<ChapterStage, string> = {
  imported: '待读', read: '已读', attributed: '已归因', verified: '已校验', rendered: '已渲染', qa: '已验收',
};

export const gateLabels: Record<Gate['status'], string> = { pending: '待读', read: '已读', attributed: '已归因', verified: '已校验' };

export const qaVerdictLabels: Record<QARecord['verdict'], string> = { pass: '通过', fail: '不通过', unchecked: '未验收' };

export const gateStatus = (draft: Draft): Gate['status'] => draft.gate?.status ?? 'pending';

export const qaVerdict = (draft: Draft): QARecord['verdict'] => draft.qa?.verdict ?? 'unchecked';

/** 已经渲染过的作品：任一生成版本以作品 ID 作为 settings.id 即说明这一章渲染过。 */
export function renderedDraftIds(history: Generation[]): Set<string> {
  return new Set(history.map(item => item.settings.id));
}

/** 章节阶段：验收记录优先，其次任一生成版本，最后看闸门。 */
export function chapterStage(draft: Draft, rendered: Set<string>): ChapterStage {
  const verdict = draft.qa?.verdict;
  if (verdict === 'pass' || verdict === 'fail') return 'qa';
  if (rendered.has(draft.id)) return 'rendered';
  switch (draft.gate?.status) {
    case 'read': return 'read';
    case 'attributed': return 'attributed';
    case 'verified': return 'verified';
    default: return 'imported';
  }
}

/** 未完成动作的顺序：无闸门→该读、已读未校验→该校验、已验证未渲染、已渲染未验收。 */
export type ShelfAction = 'read' | 'verify' | 'render' | 'qa';

export const actionLabels: Record<ShelfAction, string> = { read: '去读', verify: '去校验', render: '去渲染', qa: '去验收' };

export function nextAction(stage: ChapterStage): ShelfAction | undefined {
  switch (stage) {
    case 'imported': return 'read';
    case 'read':
    case 'attributed': return 'verify';
    case 'verified': return 'render';
    case 'rendered': return 'qa';
    default: return undefined;
  }
}

export interface ShelfChapter { draft: Draft; number: number; stage: ChapterStage }

export type StageCounts = Record<ChapterStage, number>;

export interface ShelfBook { book: Book; chapters: ShelfChapter[]; counts: StageCounts; done: number }

export interface ShelfGroup { id: string; title: string; books: ShelfBook[] }

export interface ShelfTask { book: Book; chapter: ShelfChapter; action: ShelfAction; /** 服务端队列给出的说明；本地派生时没有。 */ reason?: string }

/** 默认卷的标识与 Go 的 schema.UnfiledBookID 一致；没有归属的章节落到这里。 */
export const unfiledBookId = 'unfiled';

const chapterRank = (draft: Draft) => draft.chapter && draft.chapter > 0 ? draft.chapter : Number.MAX_SAFE_INTEGER;
const chapterOrder = (a: Draft, b: Draft) => chapterRank(a) - chapterRank(b) || a.title.localeCompare(b.title);
const bookOrder = (a: Book, b: Book) => (a.order ?? 0) - (b.order ?? 0) || a.title.localeCompare(b.title);

function shelfBook(book: Book, drafts: Draft[], rendered: Set<string>): ShelfBook {
  const chapters = [...drafts].sort(chapterOrder).map((draft, index): ShelfChapter => ({
    draft,
    number: draft.chapter && draft.chapter > 0 ? draft.chapter : index + 1,
    stage: chapterStage(draft, rendered),
  }));
  const counts: StageCounts = { imported: 0, read: 0, attributed: 0, verified: 0, rendered: 0, qa: 0 };
  for (const chapter of chapters) counts[chapter.stage]++;
  return { book, chapters, counts, done: counts.qa };
}

/**
 * 按系列归组书架：系列按状态里的顺序，系列内的书按卷序；没有系列或系列不存在的书
 * 放进“未分系列”。没有 bookId 或书已不存在的章节归到“未归档”卷。
 */
export function buildShelf(books: Book[] | undefined, series: Series[] | undefined, drafts: Draft[], history: Generation[]): ShelfGroup[] {
  const rendered = renderedDraftIds(history);
  const bookList = books ?? [];
  const seriesList = series ?? [];
  const known = new Set(bookList.map(book => book.id));
  const byBook = new Map<string, Draft[]>(bookList.map(book => [book.id, []]));
  for (const draft of drafts) {
    if (projectKind(draft) !== 'story') continue;
    const id = draft.bookId && known.has(draft.bookId) ? draft.bookId : unfiledBookId;
    const list = byBook.get(id) ?? [];
    list.push(draft);
    byBook.set(id, list);
  }

  const groups: ShelfGroup[] = [];
  for (const item of seriesList) {
    const items = bookList.filter(book => book.seriesId === item.id).sort(bookOrder);
    if (items.length) groups.push({ id: item.id, title: item.title, books: items.map(book => shelfBook(book, byBook.get(book.id) ?? [], rendered)) });
  }
  const standalone = bookList.filter(book => !book.seriesId || !seriesList.some(item => item.id === book.seriesId)).sort(bookOrder);
  // 没有默认卷但有未归档章节时补一个；后端始终保留这个卷，旧数据导入前预览也能读到章节。
  const unfiled = standalone.find(book => book.id === unfiledBookId) ?? (byBook.has(unfiledBookId) ? { id: unfiledBookId, title: '未归档' } : undefined);
  if (unfiled && !standalone.includes(unfiled)) standalone.push(unfiled);
  if (standalone.length) groups.push({ id: '', title: '未分系列', books: standalone.map(book => shelfBook(book, byBook.get(book.id) ?? [], rendered)) });
  return groups;
}

/** 服务端队列的 kind 与书架动作同名；未知值（将来新增的类型）不渲染。 */
export function isShelfAction(value: string): value is ShelfAction {
  return value === 'read' || value === 'verify' || value === 'render' || value === 'qa';
}

/**
 * 把服务端待办映射到书架里的章节：按 queue.pending 的先后顺序保留，
 * 找不到的章节（刚删除或不属于书架）跳过，不用本地动作补齐。
 */
export function queueTasks(groups: ShelfGroup[], items: QueueItem[]): ShelfTask[] {
  const byDraft = new Map<string, { book: Book; chapter: ShelfChapter }>();
  for (const group of groups) for (const item of group.books) for (const chapter of item.chapters) {
    byDraft.set(chapter.draft.id, { book: item.book, chapter });
  }
  return items.flatMap(item => {
    const target = byDraft.get(item.draftId);
    if (!target || !isShelfAction(item.kind)) return [];
    return [{ ...target, action: item.kind, reason: item.reason }];
  });
}

/** 全书架按阅读顺序排列的未完成动作；界面只展示最前面的几件。 */
export function shelfTasks(groups: ShelfGroup[]): ShelfTask[] {
  const tasks: ShelfTask[] = [];
  for (const group of groups) for (const item of group.books) for (const chapter of item.chapters) {
    const action = nextAction(chapter.stage);
    if (action) tasks.push({ book: item.book, chapter, action });
  }
  return tasks;
}

/** 该读与该校验去归因阅读页；渲染与验收先去章节工作区（验收页尚未实现）。 */
export function taskTarget(task: ShelfTask): 'reading' | 'create' {
  return (task.action === 'read' || task.action === 'verify') && task.chapter.draft.subtitles ? 'reading' : 'create';
}
