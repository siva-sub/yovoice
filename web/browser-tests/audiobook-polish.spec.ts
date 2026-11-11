import { test, expect, type Page } from '@playwright/test';
import { seedState } from './seed';
import { emptyState, type Draft, type Generation, type QARecord, type State } from '../src/shared/workbench';

const gate = (status: 'pending' | 'read' | 'attributed' | 'verified'): Draft['gate'] => ({
  status,
  textSha: status === 'pending' ? '' : '0123456789abcdef',
  ...(status === 'verified' ? { verifiedAt: '2026-11-11T00:00:00Z' } : {}),
});

const qa = (verdict: QARecord['verdict']): QARecord => ({ wer: 0.02, lufs: -18.9, peak: -3.1, silences: 1, verdict });

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
  id: draft.id, title: draft.title, fileName: 'rendered.wav', createdAt: '2026-11-11T00:00:00Z', duration: 12,
  settings: { ...draft, subtitles: undefined, text: '' },
});

// 五章覆盖待读、已读、已校验、已渲染与已验收；另有空卷与空系列，用来验证不画空框。
function shelfSeed(): State {
  const state = emptyState();
  state.series = [{ id: 'shadow', title: 'Shadow Slave' }, { id: 'empty', title: '空系列' }];
  state.books = [
    { id: 'vol6', title: '第六卷', seriesId: 'shadow', order: 6 },
    { id: 'vol7', title: '第七卷 空卷', seriesId: 'shadow', order: 7 },
    { id: 'spinoff', title: '外传 空卷', seriesId: 'empty' },
    { id: 'unfiled', title: '未归档' },
  ];
  const chapters = [
    chapter('1'.repeat(32), '第一章 相遇', 1, { gate: gate('pending') }),
    chapter('2'.repeat(32), '第二章 启程与一段足够长的标题用来验证不会撑破排版', 2, { gate: gate('read') }),
    chapter('3'.repeat(32), '第三章 风暴', 3, { gate: gate('verified') }),
    chapter('4'.repeat(32), '第四章 回航', 4, { gate: gate('verified'), qa: qa('unchecked') }),
    chapter('5'.repeat(32), '第五章 验收', 5, { gate: gate('verified'), qa: qa('pass') }),
  ];
  state.drafts = chapters;
  state.history = [rendered(chapters[3]), rendered(chapters[4])];
  return state;
}

async function openShelf(page: Page) {
  await page.goto('/');
  await page.getByTestId('nav-shelf').click();
  await expect(page.getByTestId('audiobook-shelf')).toBeVisible();
}

test('书架进度条与数字一致，空卷与空系列不画空框', async ({ page }) => {
  await seedState(page, shelfSeed());
  await openShelf(page);
  const book = page.getByTestId('audiobook-book').filter({ hasText: '第六卷' });
  await expect(book.getByTestId('audiobook-progress')).toContainText('已验收 1/5');
  const bar = book.getByTestId('audiobook-progress-bar').getByRole('progressbar');
  await expect(bar).toHaveAttribute('aria-valuenow', '1');
  await expect(bar).toHaveAttribute('aria-valuemax', '5');
  // 有书但没有章节的卷、以及整组都没有章节的系列都不渲染。
  await expect(page.getByTestId('audiobook-book').filter({ hasText: '第七卷 空卷' })).toHaveCount(0);
  await expect(page.getByTestId('audiobook-shelf-series').filter({ hasText: '空系列' })).toHaveCount(0);
});

test('章节列表用上下键移动选中，回车打开该章', async ({ page }) => {
  await seedState(page, shelfSeed());
  await openShelf(page);
  const rows = page.getByTestId('audiobook-chapter');
  const buttons = page.locator('.audiobook-chapter-open');
  await expect(rows).toHaveCount(5);
  await buttons.first().focus();
  await page.keyboard.press('ArrowDown');
  await expect(buttons.nth(1)).toBeFocused();
  await expect(rows.nth(1)).toHaveAttribute('data-active', 'true');
  await page.keyboard.press('Enter');
  await expect(page.locator('.audiobook-reading')).toBeVisible();
  await expect(page.getByTestId('nav-reading')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('.audiobook-reading').getByRole('heading', { name: /第二章 启程/ })).toBeVisible();
});

test('阅读页键盘帮助可以打开与关闭，页头显示闸门状态', async ({ page }) => {
  await seedState(page, shelfSeed());
  await openShelf(page);
  await page.locator('.audiobook-chapter-open').first().click();
  await expect(page.locator('.audiobook-reading')).toBeVisible();
  await expect(page.getByTestId('audiobook-gate-status')).toContainText('闸门：待读');
  await expect(page.getByTestId('audiobook-confirm-read')).toBeEnabled();
  await expect(page.getByTestId('audiobook-verify')).toBeDisabled();
  await expect(page.getByTestId('audiobook-keyboard-help')).toHaveCount(0);

  await page.getByTestId('audiobook-keyboard-help-toggle').click();
  const help = page.getByTestId('audiobook-keyboard-help');
  await expect(help).toBeVisible();
  await expect(help).toContainText('改派给对应说话人');
  await expect(help).toContainText('改派给旁白');
  await expect(help).toContainText('下一处复核');
  await expect(help).toContainText('上一处复核');
  await expect(help).toContainText('接受当前归因');
  await expect(help).toContainText('撤销上一次改派');

  await page.getByTestId('audiobook-keyboard-help-close').click();
  await expect(help).toHaveCount(0);

  // 行密度两档：紧凑收紧行距，舒适恢复；选择写入本机偏好，不写作品状态。
  const cues = page.getByTestId('audiobook-cues');
  await page.getByRole('radio', { name: '紧凑', exact: true }).click();
  await expect(cues).toHaveAttribute('data-density', 'compact');
  await page.getByRole('radio', { name: '舒适', exact: true }).click();
  await expect(cues).toHaveAttribute('data-density', 'comfortable');
});

test('900px 窄屏下书架与阅读页都不横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 });
  await seedState(page, shelfSeed());
  await openShelf(page);
  const overflow = (selector: string) => page.locator(selector).evaluate(element => element.scrollWidth - element.clientWidth);
  expect(await overflow('.audiobook-shelf-content')).toBeLessThanOrEqual(1);

  await page.locator('.audiobook-chapter-open').first().click();
  await expect(page.locator('.audiobook-reading')).toBeVisible();
  expect(await overflow('.audiobook-page')).toBeLessThanOrEqual(1);
  expect(await overflow('.audiobook-reading')).toBeLessThanOrEqual(1);
  expect(await overflow('.audiobook-cues')).toBeLessThanOrEqual(1);
  const viewport = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: window.innerWidth }));
  expect(viewport.scroll).toBeLessThanOrEqual(viewport.width + 1);
});

test('助手在书架与阅读页给出该页专属的快捷动作', async ({ page }) => {
  await seedState(page, shelfSeed());
  await openShelf(page);
  await page.getByTestId('assistant-toggle').click();
  const panel = page.getByTestId('assistant-panel');
  await expect(panel.getByRole('button', { name: '列出待办章', exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: '下一件事', exact: true })).toBeVisible();
  await panel.getByTestId('assistant-minimize').click();

  await page.locator('.audiobook-chapter-open').first().click();
  await expect(page.locator('.audiobook-reading')).toBeVisible();
  await page.getByTestId('assistant-toggle').click();
  await expect(panel.getByRole('button', { name: '校验这一章', exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: '找出可疑句', exact: true })).toBeVisible();
});
