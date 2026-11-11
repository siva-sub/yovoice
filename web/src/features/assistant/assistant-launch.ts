import type { TranslatorFn } from '@astryxdesign/core/i18n';
import type { QuickAction } from './assistant-panel';

// 随当前页面变化的快捷胶囊：点了即发送一条自然语言指令。
export function quickActions(t: TranslatorFn, page: string): QuickAction[] {
  const actions: Record<string, { label: string; prompt: string }[]> = {
    score: [
      { label: t('@yovoice.assistant.chip.piano'), prompt: t('@yovoice.assistant.chip.pianoPrompt') },
      { label: t('@yovoice.assistant.chip.minor'), prompt: t('@yovoice.assistant.chip.minorPrompt') },
    ],
    music: [
      { label: t('@yovoice.assistant.chip.song'), prompt: t('@yovoice.assistant.chip.songPrompt') },
      { label: t('@yovoice.assistant.chip.style'), prompt: t('@yovoice.assistant.chip.stylePrompt') },
    ],
    sfx: [
      { label: t('@yovoice.assistant.chip.sfx'), prompt: t('@yovoice.assistant.chip.sfxPrompt') },
    ],
    text: [
      { label: t('@yovoice.assistant.chip.voice'), prompt: t('@yovoice.assistant.chip.voicePrompt') },
      { label: t('@yovoice.assistant.chip.polish'), prompt: t('@yovoice.assistant.chip.polishPrompt') },
    ],
    story: [
      { label: t('@yovoice.assistant.chip.assign'), prompt: t('@yovoice.assistant.chip.assignPrompt') },
      { label: t('@yovoice.assistant.chip.dub'), prompt: t('@yovoice.assistant.chip.dubPrompt') },
    ],
    shelf: [
      { label: t('@yovoice.assistant.chip.pending'), prompt: t('@yovoice.assistant.chip.pendingPrompt') },
      { label: t('@yovoice.assistant.chip.next'), prompt: t('@yovoice.assistant.chip.nextPrompt') },
    ],
    reading: [
      { label: t('@yovoice.assistant.chip.verifyChapter'), prompt: t('@yovoice.assistant.chip.verifyChapterPrompt') },
      { label: t('@yovoice.assistant.chip.suspect'), prompt: t('@yovoice.assistant.chip.suspectPrompt') },
    ],
  };
  return actions[page] ?? actions.text;
}
