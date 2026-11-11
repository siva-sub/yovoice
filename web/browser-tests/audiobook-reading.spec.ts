import { test, expect, type Page } from '@playwright/test';
import { cueKind, evidenceMark, reviewQueue, reviewTarget, speakerKeys } from '../src/features/audiobook/reading-model';
import type { SubtitleDocument } from '../src/shared/workbench';

// 阅读页接进导航前由本文件单独挂载；串行执行，避免多个页面同时触发 Vite 预构建后互相刷新。
test.describe.configure({ mode: 'serial' });

const chapter: SubtitleDocument = {
  speakers: [
    { id: 'sunny', sourceName: 'Sunny' },
    { id: 'cor', sourceName: 'Cor' },
    { id: 'jet', sourceName: 'Jet' },
  ],
  cues: [
    { id: 'c1', start: 0, end: 10000, speakerId: '', kind: 'narration', text: 'The old man shifted his gaze.' },
    { id: 'c2', start: 10000, end: 12000, speakerId: 'sunny', confidence: 0.42, evidence: 'lead_next', kind: 'dialogue', text: '"What about you, young man?"' },
    { id: 'c3', start: 12000, end: 14000, speakerId: '', confidence: 0.9, evidence: 'prose', kind: 'narration', text: 'Sunny shifted uncomfortably, suspecting a hidden meaning.' },
    { id: 'c4', start: 14000, end: 16000, speakerId: 'cor', confidence: 0.31, evidence: 'alternation', kind: 'dialogue', text: '"I feel like it is a good fit for my abilities."' },
  ],
};

test('说话人键位按首次出场固定，复核队列在本章内前进与回绕', () => {
  expect(speakerKeys(chapter.speakers, chapter.cues)).toEqual([
    { key: '1', speakerId: 'sunny', name: 'Sunny' },
    { key: '2', speakerId: 'cor', name: 'Cor' },
  ]);
  expect(reviewQueue(chapter.cues)).toEqual([1, 3]);
  expect(reviewTarget(chapter.cues, null, 1)).toBe(1);
  expect(reviewTarget(chapter.cues, 1, 1)).toBe(3);
  expect(reviewTarget(chapter.cues, 3, 1)).toBe(1);
  expect(reviewTarget(chapter.cues, 3, -1)).toBe(1);
  expect(reviewTarget(chapter.cues, 0, 1)).toBe(1);
  // 没有 kind 的旧数据按引号判断对白，其余按叙述。
  expect(cueKind({ start: 0, end: 0, speakerId: '', text: '“请给我画一只羊。”' })).toBe('dialogue');
  expect(cueKind({ start: 0, end: 0, speakerId: '', text: '他走进房间。' })).toBe('narration');
  expect(cueKind({ start: 0, end: 0, speakerId: '', text: '心声', kind: 'thought' })).toBe('thought');
  expect(evidenceMark('Sunny', 0.62, 'lead_next')).toBe('<sunny 0.62 lead_next>');
});

// 先加载一次阅读页模块：首次动态引入新依赖时 Vite 会重新预构建并整页刷新，等刷新后再挂载。
async function warmReadingModule(page: Page) {
  await page.goto('/');
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await page.evaluate(async () => {
        const viewPath = '/src/features/audiobook/reading-view.tsx';
        await import(/* @vite-ignore */ viewPath);
      });
      await page.waitForTimeout(500);
      return;
    } catch {
      await page.waitForLoadState('domcontentloaded').catch(() => undefined);
      await page.waitForTimeout(500);
    }
  }
}

// 把阅读视图挂到页面上；接进导航前用它单独验证组件，样式与令牌仍来自应用的样式表。
async function mountReading(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await warmReadingModule(page);
  await page.goto('/');
  await page.evaluate(async chapter => {
    const reactPath = '/@id/react', clientPath = '/@id/react-dom/client', viewPath = '/src/features/audiobook/reading-view.tsx';
    const React = (await import(/* @vite-ignore */ reactPath)).default;
    const { createRoot } = (await import(/* @vite-ignore */ clientPath)).default;
    const { AudiobookReadingView } = await import(/* @vite-ignore */ viewPath);
    const changes: unknown[] = [];
    Object.assign(window, { __audiobookChanges: changes });
    const host = document.createElement('main');
    host.id = 'audiobook-test-root';
    host.style.cssText = 'position: fixed; inset: 0; z-index: 2147483647; background: var(--app-paper);';
    document.body.append(host);
    createRoot(host).render(React.createElement(AudiobookReadingView, {
      chapterId: 'chapter-1', title: '第一章 相遇', subtitles: chapter,
      onCueChange: (change: unknown) => changes.push(change),
    }));
  }, chapter);
  await expect(page.locator('.audiobook-reading')).toBeVisible();
  return errors;
}

test('改派后置信度消失，撤销恢复，J/K 在复核队列里推进', async ({ page }) => {
  const errors = await mountReading(page);
  const rows = page.locator('.audiobook-cue');
  const suspect = page.locator('[data-cue-index="1"]');
  const second = page.locator('[data-cue-index="3"]');
  const status = page.getByTestId('audiobook-review-status');
  await expect(rows).toHaveCount(4);
  await expect(suspect.locator('.audiobook-evidence')).toHaveText('<sunny 0.42 lead_next>');

  // 进章节时映射固定：旁白 0，Sunny 1，Cor 2。
  await expect(page.getByText('J/K 复核 · A 接受 · Ctrl+Z 撤销')).toBeVisible();
  await expect(status).toContainText('第 0/2 处');
  await page.keyboard.press('j');
  await expect(suspect).toHaveAttribute('aria-current', 'true');
  await expect(status).toContainText('第 1/2 处');
  await page.keyboard.press('j');
  await expect(second).toHaveAttribute('aria-current', 'true');
  await expect(status).toContainText('第 2/2 处');
  await page.keyboard.press('k');
  await expect(suspect).toHaveAttribute('aria-current', 'true');
  await expect(status).toContainText('第 1/2 处');

  // 数字键改派：说话人变为 Cor，置信度与依据标记一起消失。
  await suspect.click();
  await page.keyboard.press('2');
  await expect(suspect).toContainText('Cor');
  await expect(suspect.locator('.audiobook-evidence')).toHaveCount(0);
  await expect(suspect).not.toHaveAttribute('data-confidence');
  await expect(suspect).not.toHaveAttribute('data-low-confidence', 'true');
  await expect(status).toContainText('第 0/1 处');
  const changes = await page.evaluate(() => (window as unknown as { __audiobookChanges: Record<string, unknown>[] }).__audiobookChanges);
  expect(changes.at(-1)).toMatchObject({ index: 1, cueId: 'c2', speakerId: 'cor' });
  expect(changes.at(-1)?.confidence).toBeUndefined();

  // Ctrl+Z 撤销：说话人与置信度一起恢复，队列回到两处。
  await page.keyboard.press('Control+z');
  await expect(suspect).toContainText('Sunny');
  await expect(suspect.locator('.audiobook-evidence')).toHaveText('<sunny 0.42 lead_next>');
  await expect(status).toContainText('第 1/2 处');

  // A 接受同样清空置信度，但不改说话人。
  await page.keyboard.press('a');
  await expect(suspect).toContainText('Sunny');
  await expect(suspect.locator('.audiobook-evidence')).toHaveCount(0);
  await expect(page.locator('[data-cue-index="3"] .audiobook-evidence')).toHaveText('<cor 0.31 alternation>');

  // 正文默认两行，点击展开、再点收起。
  const longText = page.locator('[data-cue-index="2"] .audiobook-text');
  await expect(longText).toHaveAttribute('data-expanded', 'false');
  await longText.click();
  await expect(longText).toHaveAttribute('data-expanded', 'true');
  await longText.click();
  await expect(longText).toHaveAttribute('data-expanded', 'false');
  expect(errors).toEqual([]);
});

test('提词器试听按顺序高亮当前句', async ({ page }) => {
  await mountReading(page);
  const first = page.locator('[data-cue-index="0"]');
  const second = page.locator('[data-cue-index="1"]');
  await expect(first).not.toHaveAttribute('data-prompting', 'true');
  await page.getByRole('button', { name: '提词器试听' }).click();
  await expect(page.getByText(/正在试听：第 1 句 · 旁白/)).toBeVisible();
  await expect(first).toHaveAttribute('data-prompting', 'true');
  await page.getByRole('button', { name: '停止试听' }).click();
  await expect(first).not.toHaveAttribute('data-prompting', 'true');
  await expect(second).not.toHaveAttribute('data-prompting', 'true');
});
