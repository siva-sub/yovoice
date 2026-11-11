import { test, expect, type Page } from '@playwright/test';
import { seedState } from './seed';
import { emptyState, synthesisSettings, type Character, type State, type SubtitleDocument, type Voice } from '../src/shared/workbench';
import { auditChapterDialogue, scriptChapters } from '../src/features/create/manuscript';

// 阅读页的两类编辑：D/N/T 改类型与 V 选区批量改派。
// 浏览器预览没有 draft.reassignCues，批量改派正好走“逐条修改并提示”的降级路径。
const chapter: SubtitleDocument = {
  speakers: [
    { id: 'sunny', sourceName: 'Sunny' },
    { id: 'cor', sourceName: 'Cor' },
  ],
  cues: [
    { id: 'c1', start: 0, end: 10000, speakerId: '', kind: 'narration', text: 'The old man shifted his gaze.' },
    { id: 'c2', start: 10000, end: 12000, speakerId: 'sunny', confidence: 0.42, evidence: 'lead_next', kind: 'dialogue', text: '"What about you, young man?"' },
    { id: 'c3', start: 12000, end: 14000, speakerId: '', confidence: 0.9, evidence: 'prose', kind: 'narration', text: 'Sunny shifted uncomfortably.' },
    { id: 'c4', start: 14000, end: 16000, speakerId: 'cor', confidence: 0.31, evidence: 'alternation', kind: 'dialogue', text: '"I feel like it is a good fit."' },
  ],
};

const draftId = 'a'.repeat(32);

function chapterSeed(): State {
  const state = emptyState();
  state.drafts = [{
    ...state.drafts[0], id: draftId, title: '第一章 相遇', kind: 'story',
    text: chapter.cues.map(cue => cue.text).join('\n'), subtitles: chapter,
  }];
  return state;
}

async function openReading(page: Page) {
  await seedState(page, chapterSeed());
  await page.goto('/');
  await page.getByTestId('nav-reading').click();
  await expect(page.locator('.audiobook-reading')).toBeVisible();
}

// 读回自动保存到本机状态里的这一章字幕。
async function savedCues(page: Page) {
  return page.evaluate(id => {
    const saved = JSON.parse(localStorage.getItem('voice-workbench-v1')!);
    return saved.drafts.find((item: { id: string }) => item.id === id)?.subtitles?.cues as
      { speakerId: string; kind?: string; confidence?: number }[];
  }, draftId);
}

test('D/N/T 改类型并清空置信度，撤销恢复原来的类型与置信度', async ({ page }) => {
  await openReading(page);
  const dialogue = page.locator('.audiobook-cue[data-cue-index="1"]');
  const narration = page.locator('.audiobook-cue[data-cue-index="2"]');
  await expect(dialogue.locator('.audiobook-kind')).toHaveText('"');
  await expect(narration.locator('.audiobook-kind')).toHaveText('~');

  // D 把叙述改成对白：类型标记变化，置信度与依据标记一起消失，说话人不变。
  await narration.locator('.audiobook-index').click();
  await page.keyboard.press('d');
  await expect(narration.locator('.audiobook-kind')).toHaveText('"');
  await expect(narration).toContainText('旁白');
  await expect(narration.locator('.audiobook-evidence')).toHaveCount(0);
  await expect(narration).not.toHaveAttribute('data-confidence');
  await expect.poll(async () => (await savedCues(page))[2]?.kind).toBe('dialogue');
  await expect.poll(async () => (await savedCues(page))[2]?.confidence).toBeUndefined();

  // 撤销恢复原类型与原置信度。
  await page.keyboard.press('Control+z');
  await expect(narration.locator('.audiobook-kind')).toHaveText('~');
  await expect(narration.locator('.audiobook-evidence')).toHaveText('<旁白 0.90 prose>');

  // N 把对白改成叙述，同样清空置信度。
  await dialogue.locator('.audiobook-index').click();
  await page.keyboard.press('n');
  await expect(dialogue.locator('.audiobook-kind')).toHaveText('~');
  await expect(dialogue).toContainText('Sunny');
  await expect(dialogue.locator('.audiobook-evidence')).toHaveCount(0);
  await expect.poll(async () => (await savedCues(page))[1]?.kind).toBe('narration');

  // T 设为心声。
  await page.keyboard.press('t');
  await expect(dialogue.locator('.audiobook-kind')).toHaveText('·');
  await expect(dialogue.locator('.audiobook-evidence')).toHaveCount(0);
});

test('V 选区用 J/K 扩展（含逆序），动作条批量改派只影响选中的行', async ({ page }) => {
  await openReading(page);
  const rows = page.locator('.audiobook-cue');

  // 从第 1 句往下扩两句：第 1–3 句进入选区。
  await rows.nth(0).locator('.audiobook-index').click();
  await page.keyboard.press('v');
  await page.keyboard.press('j');
  await page.keyboard.press('j');
  await expect(page.getByTestId('audiobook-selection')).toContainText('已选 3 句');
  for (const index of [0, 1, 2]) await expect(rows.nth(index)).toHaveAttribute('data-selected', 'true');
  await expect(rows.nth(3)).not.toHaveAttribute('data-selected', 'true');

  // 回车弹出动作条；设为 Sunny 后只有选中的三句改派，第 4 句保持 Cor。
  await page.keyboard.press('Enter');
  const bar = page.getByTestId('audiobook-selection-bar');
  await expect(bar).toBeVisible();
  await bar.getByRole('button', { name: '设为 Sunny' }).click();
  for (const index of [0, 1, 2]) await expect(rows.nth(index)).toContainText('Sunny');
  await expect(rows.nth(3)).toContainText('Cor');
  await expect(rows.nth(1).locator('.audiobook-evidence')).toHaveCount(0);
  await expect(rows.nth(2).locator('.audiobook-evidence')).toHaveCount(0);
  // 预览后端没有 draft.reassignCues：本地已逐条改完，并明确提示。
  await expect(page.getByTestId('audiobook-batch-notice')).toHaveText('批量接口未接，已逐条修改');
  await expect.poll(async () => (await savedCues(page)).map(cue => cue.speakerId)).toEqual(['sunny', 'sunny', 'sunny', 'cor']);

  // 逆序选区：从第 4 句往上扩两句，得到第 2–4 句。批量改类型只影响这三句。
  await rows.nth(3).locator('.audiobook-index').click();
  await page.keyboard.press('v');
  await page.keyboard.press('k');
  await page.keyboard.press('k');
  await expect(page.getByTestId('audiobook-selection')).toContainText('已选 3 句');
  await expect(rows.nth(0)).not.toHaveAttribute('data-selected', 'true');
  for (const index of [1, 2, 3]) await expect(rows.nth(index)).toHaveAttribute('data-selected', 'true');
  await page.keyboard.press('Enter');
  await page.getByTestId('audiobook-selection-bar').getByRole('button', { name: '设为对白' }).click();
  await expect(rows.nth(0).locator('.audiobook-kind')).toHaveText('~');
  await expect(rows.nth(2).locator('.audiobook-kind')).toHaveText('"');
  // 只改类型不换说话人：第 4 句仍是 Cor。
  await expect(rows.nth(3)).toContainText('Cor');
  await expect(page.getByTestId('audiobook-batch-notice')).toHaveText('批量接口未接，已逐条修改');
});

test('导入自检对一个人造样本报出三类异常，导入对话框只提示不阻断', async ({ page }) => {
  // 纯逻辑：三类异常各一例。对白行数是解析器的产出，样本里直接给出来，模拟解析器漏读或多读。
  const sample = [
    { title: '漏掉整章对白', text: '他压低声音：“走吧。”她没有回答：“好。”' },
    { title: '只剩一句对白', text: '“一。”“二。”“三。”“四。”' },
    { title: '无中生有', text: '他走进房间，关上门。' },
  ];
  const lines = [
    [],
    [{ text: '一。', quote: true, speaker: 'a' }],
    [{ text: '他走进房间，关上门。', quote: false, speaker: '' }, { text: '他走进房间，关上门。', quote: true, speaker: 'a' }],
  ];
  expect(sample.map((item, index) => auditChapterDialogue(item, lines[index]).issue)).toEqual(['missed', 'thin', 'fabricated']);

  // 同一个口径也认得规则解析器真正漏掉的情形：引号里只有标点时，对白行会被过滤掉。
  const [parsed] = scriptChapters([{ title: '只剩一句', text: '“……” “……” “……” “好。”' }], '?');
  expect(auditChapterDialogue({ title: '只剩一句', text: '“……” “……” “……” “好。”' }, parsed.lines).issue).toBe('thin');

  // 界面上：章节列表旁标出异常，勾选与“下一步”保持可用。
  await seedState(page, emptyState());
  await page.goto('/');
  await page.getByTestId('nav-story').click();
  const novel = '第一章 漏掉对白\n“……”\n\n第二章 只剩一句\n“……” “……” “……” “好。”\n\n第三章 正常\n他走进房间，关上门。';
  await page.getByLabel('小说文件').first().setInputFiles({ name: '自检样本.txt', mimeType: 'text/plain', buffer: Buffer.from(novel) });
  const dialog = page.getByRole('dialog', { name: '导入小说' });
  const flags = dialog.getByTestId('book-chapter-audit');
  await expect(flags).toHaveCount(2);
  await expect(flags.nth(0)).toHaveText('MISSED?');
  await expect(flags.nth(0)).toHaveAttribute('data-issue', 'missed');
  await expect(flags.nth(1)).toHaveText('THIN?');
  await expect(flags.nth(1)).toHaveAttribute('data-issue', 'thin');
  await expect(dialog.getByTestId('book-import-audit-note')).toContainText('不阻止导入');
  // 标了异常的章节仍可勾选，导入不被阻断。
  await dialog.getByRole('checkbox', { name: '第一章 漏掉对白' }).check();
  await expect(dialog.getByRole('button', { name: '下一步：分角色' })).toBeEnabled();
});

const voice = (id: string, name: string, duration: number): Voice => ({ id, name, fileName: `${id}.wav`, duration });

function castSeed(): State {
  const state = emptyState();
  const settings = synthesisSettings(state.drafts[0]);
  const long = voice('a'.repeat(32), 'Sunny 长锚点', 12);
  const short = voice('b'.repeat(32), 'Cor 短锚点', 6);
  state.voices = [long, short];
  state.characters = [
    { id: 'c'.repeat(32), name: 'Sunny', demoText: 'The sea stretched on.', settings: { ...settings, voiceId: long.id, voiceDescription: 'A quiet young man.' }, createdAt: '2026-11-11T00:00:00Z', updatedAt: '2026-11-11T00:00:00Z' },
    { id: 'd'.repeat(32), name: 'Cor', demoText: 'A good fit.', settings: { ...settings, voiceId: short.id, voiceDescription: 'A steady voice.' }, createdAt: '2026-11-11T00:00:00Z', updatedAt: '2026-11-11T00:00:00Z' },
  ] satisfies Character[];
  return state;
}

test('角色页对超过 10 秒的锚点显示退回 eager 的告警', async ({ page }) => {
  await seedState(page, castSeed());
  await page.goto('/');
  await page.getByTestId('nav-cast').click();
  const warnings = page.getByTestId('audiobook-anchor-warning');
  await expect(warnings).toHaveCount(1);
  await expect(warnings.first()).toContainText('超过约 10 秒');
  await expect(warnings.first()).toContainText('eager');
  await expect(warnings.first()).toContainText('4 倍');
  // 6 秒的短锚点不告警。
  const shortCard = page.getByTestId('audiobook-cast-card').filter({ hasText: 'Cor' });
  await expect(shortCard.getByTestId('audiobook-anchor-warning')).toHaveCount(0);
});
