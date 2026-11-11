import { test, expect } from '@playwright/test';
import { seedState } from './seed';
import { emptyState, synthesisSettings, type Character, type Draft, type Generation, type QARecord, type State, type Voice } from '../src/shared/workbench';
import { buildShelf, shelfTasks } from '../src/features/audiobook/shelf-model';

const gate = (status: 'pending' | 'read' | 'attributed' | 'verified'): Draft['gate'] => ({
  status,
  textSha: status === 'pending' ? '' : '0123456789abcdef',
  ...(status === 'verified' ? { verifiedAt: '2026-11-11T00:00:00Z' } : {}),
});

const qa = (verdict: QARecord['verdict']): QARecord => ({
  wer: 0.02, lufs: -18.9, peak: -3.1, silences: 1, verdict,
  ...(verdict === 'fail' ? { note: '第 3 句吞词' } : {}),
});

function chapter(id: string, title: string, chapterNumber: number, patch: Partial<Draft> = {}): Draft {
  return {
    ...emptyState().drafts[0], id, title, kind: 'story', chapter: chapterNumber, bookId: 'vol6',
    text: `${title}\n正文。`,
    subtitles: {
      speakers: [{ id: 'narrator', sourceName: '旁白' }],
      cues: [{ id: `${id}-cue`, start: 0, end: 1000, text: '正文。', speakerId: '' }],
    },
    ...patch,
  };
}

const rendered = (draft: Draft): Generation => ({
  id: '9'.repeat(32), title: draft.title, fileName: 'rendered.wav', createdAt: '2026-11-11T00:00:00Z', duration: 12,
  settings: { ...draft, subtitles: undefined, text: '' },
});

function shelfSeed(): State {
  const state = emptyState();
  state.series = [{ id: 'shadow', title: 'Shadow Slave' }];
  state.books = [
    { id: 'vol6', title: '第六卷', seriesId: 'shadow', order: 6 },
    { id: 'unfiled', title: '未归档' },
  ];
  const chapters = [
    chapter('1'.repeat(32), '第一章 相遇', 1, { gate: gate('pending') }),
    chapter('2'.repeat(32), '第二章 启程', 2, { gate: gate('read') }),
    chapter('3'.repeat(32), '第三章 风暴', 3, { gate: gate('verified') }),
    chapter('4'.repeat(32), '第四章 回航', 4, { gate: gate('verified'), qa: qa('unchecked') }),
    chapter('5'.repeat(32), '第五章 验收', 5, { gate: gate('verified'), qa: qa('pass') }),
  ];
  state.drafts = chapters;
  state.history = [rendered(chapters[3]), rendered(chapters[4])];
  return state;
}

function castSeed(): State {
  const state = emptyState();
  const voice: Voice = { id: 'a'.repeat(32), name: 'Sunny 锚点', fileName: 'sunny.wav', duration: 12 };
  const settings = synthesisSettings(emptyState().drafts[0]);
  const character: Character = {
    id: 'b'.repeat(32), name: 'Sunny', demoText: 'The sea stretched on.',
    settings: { ...settings, voiceId: voice.id, voiceDescription: 'A quiet young man with a low, even voice.' },
    createdAt: '2026-11-11T00:00:00Z', updatedAt: '2026-11-11T00:00:00Z',
  };
  state.voices = [voice];
  state.characters = [character];
  state.drafts = [chapter('c'.repeat(32), '第一章 相遇', 1, {
    gate: gate('verified'), qa: qa('pass'),
    subtitles: { speakers: [{ id: 'sunny', sourceName: 'Sunny', characterId: character.id }], cues: [{ id: 'cue', start: 0, end: 1000, text: '正文。', speakerId: 'sunny' }] },
  })];
  return state;
}

test('书架阶段与下一件事从作品状态派生，只排前三个未完成动作', () => {
  const state = shelfSeed();
  const groups = buildShelf(state.books, state.series, state.drafts, state.history);
  expect(groups.map(group => group.title)).toEqual(['Shadow Slave', '未分系列']);
  expect(groups[0].books[0].chapters.map(item => item.stage)).toEqual(['imported', 'read', 'verified', 'rendered', 'qa']);
  expect(groups[0].books[0].done).toBe(1);
  expect(shelfTasks(groups).map(task => task.action)).toEqual(['read', 'verify', 'render', 'qa']);
});

test('书架按系列列出章节与进度，下一件事能跳到阅读页与章节工作区', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await seedState(page, shelfSeed());
  await page.goto('/');

  await page.getByTestId('nav-shelf').click();
  await expect(page.getByTestId('audiobook-shelf')).toBeVisible();
  await expect(page.getByTestId('nav-shelf')).toHaveAttribute('aria-current', 'page');

  // 系列与卷的归组、每章的闸门与验收状态都能读出来。
  const series = page.getByTestId('audiobook-shelf-series').filter({ hasText: 'Shadow Slave' });
  await expect(series).toBeVisible();
  const book = series.getByTestId('audiobook-book').filter({ hasText: '第六卷' });
  await expect(book).toBeVisible();
  await expect(book.getByTestId('audiobook-progress')).toContainText('已验收 1/5');
  const rows = page.getByTestId('audiobook-chapter');
  await expect(rows).toHaveCount(5);
  await expect(rows.nth(0)).toHaveAttribute('data-stage', 'imported');
  await expect(rows.nth(0)).toContainText('第一章 相遇');
  await expect(rows.nth(0).getByTestId('audiobook-chapter-gate')).toHaveText('闸门：待读');
  await expect(rows.nth(0).getByTestId('audiobook-chapter-qa')).toHaveText('验收：未验收');
  await expect(rows.nth(1).getByTestId('audiobook-chapter-gate')).toHaveText('闸门：已读');
  await expect(rows.nth(3)).toHaveAttribute('data-stage', 'rendered');
  await expect(rows.nth(4)).toHaveAttribute('data-stage', 'qa');
  await expect(rows.nth(4).getByTestId('audiobook-chapter-qa')).toHaveText('验收：通过');

  // 只有最前面的三件事：读、校验、渲染；已渲染待验收排在第四件，不在这一次的队列里。
  const tasks = page.getByTestId('audiobook-next-item');
  await expect(tasks).toHaveCount(3);
  await expect(tasks.nth(0)).toContainText('第一章 相遇');
  await expect(tasks.nth(0)).toContainText('去读');
  await expect(tasks.nth(1)).toContainText('去校验');
  await expect(tasks.nth(2)).toContainText('第三章 风暴');
  await expect(tasks.nth(2)).toContainText('去渲染');

  // 去读：打开该章的归因阅读页。
  await tasks.nth(0).getByTestId('audiobook-next-action').click();
  await expect(page.locator('.audiobook-reading')).toBeVisible();
  await expect(page.getByTestId('nav-reading')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('.audiobook-reading').getByRole('heading', { name: '第一章 相遇' })).toBeVisible();

  // 去渲染：回到书架再打开该章的章节工作区。
  await page.getByTestId('nav-shelf').click();
  await page.getByTestId('audiobook-next-item').filter({ hasText: '第三章 风暴' }).getByTestId('audiobook-next-action').click();
  await expect(page.locator('input.document-title')).toHaveValue('第三章 风暴');
  expect(errors).toEqual([]);
});

test('角色页显示角色卡与候选空位，别名与专名说明需要脚本输出', async ({ page }) => {
  await seedState(page, castSeed());
  await page.goto('/');
  await page.getByTestId('nav-cast').click();
  await expect(page.getByTestId('audiobook-cast-page')).toBeVisible();
  const card = page.getByTestId('audiobook-cast-card');
  await expect(card).toContainText('Sunny');
  await expect(card.getByTestId('audiobook-cast-design')).toHaveText('A quiet young man with a low, even voice.');
  await expect(card.getByTestId('audiobook-cast-anchor')).toHaveText('锚点：已绑定 · Sunny 锚点');
  await expect(card).toContainText('f0：未记录');
  await expect(card.getByTestId('audiobook-cast-audit')).toContainText('章节验收通过');
  await expect(card.getByTestId('audiobook-candidate-grid')).toBeVisible();
  await expect(page.getByTestId('audiobook-alias')).toContainText('还没有别名与专名数据');
  await expect(page.getByTestId('nav-cast')).toHaveAttribute('aria-current', 'page');
});
