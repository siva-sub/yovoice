import type { SubtitleCue, SubtitleSpeaker } from '../../shared/workbench';

// 归因阅读的纯逻辑：句子类型、依据标记、说话人键位与复核队列，界面只负责画出来。

/** 低于这个置信度的句子进入复核队列；人工改派与接受会把置信度清空。 */
export const reviewThreshold = 0.5;

/** 一句的类型；旧数据没有 kind 时按引号判断对白，其余按叙述。 */
export type CueKind = 'narration' | 'dialogue' | 'thought';

export function cueKind(cue: SubtitleCue): CueKind {
  if (cue.kind === 'narration' || cue.kind === 'dialogue' || cue.kind === 'thought') return cue.kind;
  return /^["“「『]/.test(cue.text.trim()) ? 'dialogue' : 'narration';
}

/** 类型标记：叙述 ~、对白 "、心声 ·。 */
export const kindMarks: Record<CueKind, string> = { narration: '~', dialogue: '"', thought: '·' };

export const kindLabels: Record<CueKind, string> = { narration: '叙述', dialogue: '对白', thought: '心声' };

export function reviewable(confidence?: number): boolean {
  return confidence !== undefined && confidence < reviewThreshold;
}

/** 依据的人话解释，给行尾标记做悬停提示。 */
export function evidenceLabel(evidence?: string): string {
  switch (evidence) {
    case 'prose': return '叙述句，默认归旁白';
    case 'inner_monologue': return '心声，按说话人的内心独白处理';
    case 'lead_next': return '下文引出这句对白';
    case 'lead_prev': return '这句对白承接上文';
    case 'tag_next': return '句后有说话人提示语';
    case 'tag_prev': return '句前有说话人提示语';
    case 'alternation': return '按对话交替推断';
    case 'addressed_inverse': return '按称呼推断为被称呼的一方';
    case 'continuity': return '沿用前文的说话人';
    default: return '规则归因的依据';
  }
}

/** 说话人显示名；旁白的 speakerId 为空串。 */
export function speakerName(speakers: SubtitleSpeaker[], speakerId: string): string {
  if (!speakerId) return '旁白';
  const speaker = speakers.find(item => item.id === speakerId);
  return speaker?.sourceName?.trim() || speaker?.id || '未知说话人';
}

/** 键位对应的说话人：按本章首次出场顺序取前九个，旁白另用 0。进入章节后固定，不随改派重排。 */
export interface SpeakerKey { key: string; speakerId: string; name: string }

export function speakerKeys(speakers: SubtitleSpeaker[], cues: SubtitleCue[]): SpeakerKey[] {
  const seen = new Set<string>();
  const keys: SpeakerKey[] = [];
  for (const cue of cues) {
    if (!cue.speakerId || seen.has(cue.speakerId)) continue;
    seen.add(cue.speakerId);
    keys.push({ key: String(keys.length + 1), speakerId: cue.speakerId, name: speakerName(speakers, cue.speakerId) });
    if (keys.length === 9) break;
  }
  return keys;
}

/** 复核队列：本章内按正文顺序排列的低置信度句子下标，不跨章合并。 */
export function reviewQueue(cues: SubtitleCue[]): number[] {
  return cues.flatMap((cue, index) => reviewable(cue.confidence) ? [index] : []);
}

/** J/K 的目标：在复核队列里前进或后退一处，到两端后回到另一端。 */
export function reviewTarget(cues: SubtitleCue[], active: number | null, direction: 1 | -1): number | null {
  const queue = reviewQueue(cues);
  if (!queue.length) return null;
  if (active === null) return direction > 0 ? queue[0] : queue[queue.length - 1];
  const position = queue.indexOf(active);
  if (position >= 0) return queue[(position + direction + queue.length) % queue.length];
  // 当前行不在队列里：从它在正文中的位置往相应方向找第一处，找不到就绕回另一端。
  const next = direction > 0
    ? queue.find(index => index > active)
    : [...queue].reverse().find(index => index < active);
  return next ?? (direction > 0 ? queue[0] : queue[queue.length - 1]);
}

/** 行尾的紧凑依据标记，例如 `<jet 0.62 lead_next>`；没有置信度时不显示。 */
export function evidenceMark(name: string, confidence: number, evidence?: string): string {
  const marker = name.trim().toLowerCase().replace(/\s+/g, '_') || 'narrator';
  return `<${marker} ${confidence.toFixed(2)}${evidence ? ` ${evidence}` : ''}>`;
}
