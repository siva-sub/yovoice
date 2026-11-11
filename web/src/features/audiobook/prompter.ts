import { useCallback, useEffect, useRef, useState } from 'react';
import type { SubtitleCue } from '../../shared/workbench';

// 提词器试听：按正文顺序逐句高亮。音频由调用方通过 onCue 注入播放；
// 没有注入时只按 cue 的时长走一遍高亮，仍可用来看句子与说话人是否对得上。
export interface Prompter {
  playing: boolean;
  /** 当前高亮的 cue 下标；未播放时为 null。 */
  index: number | null;
  start: (from?: number) => void;
  stop: () => void;
  toggle: () => void;
}

/** 一句至少停留的时间，避免零长度字幕一闪而过。 */
const minimumCueMs = 400;

export function usePrompter(cues: SubtitleCue[], onCue?: (cue: SubtitleCue, index: number) => void): Prompter {
  const [playing, setPlaying] = useState(false);
  const [index, setIndex] = useState<number | null>(null);
  const speak = useRef(onCue);
  useEffect(() => { speak.current = onCue; }, [onCue]);

  const start = useCallback((from = 0) => {
    if (!cues.length) return;
    setIndex(Math.max(0, Math.min(from, cues.length - 1)));
    setPlaying(true);
  }, [cues.length]);
  const stop = useCallback(() => { setPlaying(false); setIndex(null); }, []);
  const toggle = useCallback(() => { if (playing) stop(); else start(index ?? 0); }, [playing, index, start, stop]);

  useEffect(() => {
    if (!playing || index === null) return;
    const cue = cues[index];
    if (!cue) { setPlaying(false); setIndex(null); return; }
    speak.current?.(cue, index);
    const timer = window.setTimeout(() => {
      if (index + 1 >= cues.length) { setPlaying(false); setIndex(null); }
      else setIndex(index + 1);
    }, Math.max(minimumCueMs, cue.end - cue.start));
    return () => window.clearTimeout(timer);
  }, [playing, index, cues]);

  return { playing, index, start, stop, toggle };
}
