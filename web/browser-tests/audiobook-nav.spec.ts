import { test, expect } from '@playwright/test';
import { seedState } from './seed';
import { emptyState, type Generation, type State, type SubtitleDocument } from '../src/shared/workbench';

// 一章四句：第 2 句归 Sunny（低置信度），第 4 句归 Cor；按 2 改派成 Cor。
const chapter: SubtitleDocument = {
  speakers: [
    { id: 'sunny', sourceName: 'Sunny' },
    { id: 'cor', sourceName: 'Cor' },
  ],
  cues: [
    { id: 'c1', start: 0, end: 10000, speakerId: '', kind: 'narration', text: 'The old man shifted his gaze.' },
    { id: 'c2', start: 10000, end: 12000, speakerId: 'sunny', confidence: 0.42, evidence: 'lead_next', kind: 'dialogue', text: '"What about you, young man?"' },
    { id: 'c3', start: 12000, end: 14000, speakerId: '', kind: 'narration', text: 'Sunny shifted uncomfortably.' },
    { id: 'c4', start: 14000, end: 16000, speakerId: 'cor', confidence: 0.31, evidence: 'alternation', kind: 'dialogue', text: '"I feel like it is a good fit."' },
  ],
};

function seed(): State {
  const state = emptyState();
  const draft = {
    ...state.drafts[0], id: 'a'.repeat(32), title: '第一章 相遇', kind: 'story' as const,
    text: chapter.cues.map(cue => cue.text).join('\n'), subtitles: chapter,
  };
  const take = (id: string, createdAt: string, duration: number): Generation => ({
    id, title: '第 2 句', fileName: `${id}.wav`, createdAt, duration,
    settings: { ...draft, modelId: 'test-model-v1', subtitles: undefined, text: chapter.cues[1].text },
    segment: { cueId: 'c2', speakerId: 'sunny', speakerName: 'Sunny', batchId: 'b'.repeat(32), index: 1 },
  });
  state.drafts = [draft];
  // 同一句两条 take，最新的排在最前。
  state.history = [take('1'.repeat(32), '2026-11-10T10:00:00Z', 2), take('2'.repeat(32), '2026-11-11T10:00:00Z', 3)];
  return state;
}

test('从导航进入归因阅读，改派一句写回作品，返回不报错', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await seedState(page, seed());
  await page.goto('/');

  // 有字幕的作品才显示入口。
  await expect(page.getByTestId('nav-reading')).toBeVisible();
  await page.getByTestId('nav-reading').click();
  await expect(page.locator('.audiobook-reading')).toBeVisible();
  await expect(page.getByTestId('nav-reading')).toHaveAttribute('aria-current', 'page');

  // 第 2 句改派给 Cor（键位 2），置信度与依据标记一起消失。
  const suspect = page.locator('.audiobook-cue[data-cue-index="1"]');
  await suspect.click();
  await page.keyboard.press('2');
  await expect(suspect).toContainText('Cor');
  await expect(suspect.locator('.audiobook-evidence')).toHaveCount(0);

  // 逐句 take 按 cue 列出：两句号、模型、时长与选中状态都能看到。
  const takes = page.locator('.audiobook-take[data-cue-id="c2"]');
  await expect(takes).toHaveCount(2);
  await expect(takes.first()).toContainText('test-model-v1');
  await expect(takes.first()).toContainText('00:03');
  await page.getByRole('button', { name: '选择第 2 句的第 1 条 take' }).click();
  await expect(takes.first()).toHaveAttribute('data-selected', 'true');

  // 改派进入自动保存：作品里的这一句写回 draft.subtitles。
  await expect.poll(() => page.evaluate(id => {
    const saved = JSON.parse(localStorage.getItem('voice-workbench-v1')!);
    return saved.drafts.find((item: { id: string }) => item.id === id)?.subtitles?.cues[1]?.speakerId;
  }, 'a'.repeat(32))).toBe('cor');

  // 返回故事作品列表，隐藏后的阅读页不再占用键位，也不报错。
  await page.getByTestId('nav-story').click();
  await expect(page.locator('.audiobook-reading')).toBeHidden();
  expect(errors).toEqual([]);
});
