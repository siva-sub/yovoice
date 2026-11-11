import { useEffect, useRef, useState } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Text } from '@astryxdesign/core/Text';
import { useLocale } from '@astryxdesign/core/i18n';
import { Check, Circle, Play, Square } from 'lucide-react';
import { mediaUrl, releaseMediaUrl } from '../../shared/lib/client';
import { formatDateTime } from '../../shared/i18n/format';
import { formatTime, type Generation, type SubtitleDocument } from '../../shared/workbench';

/** 一个 cue 的 take：同一句逐次生成的记录，按生成时间倒序排列。 */
export interface CueTakes {
  cueId: string;
  /** cue 在正文里的下标，用于显示句号。 */
  index: number;
  text: string;
  takes: Generation[];
}

/** 把当前作品的逐句生成记录归到 cue 上；整章版本等没有 cueId 的记录不列。 */
export function cueTakes(subtitles: SubtitleDocument, generations: Generation[]): CueTakes[] {
  const byCue = new Map<string, Generation[]>();
  for (const item of generations) {
    const cueId = item.segment?.cueId;
    if (!cueId) continue;
    byCue.set(cueId, [...(byCue.get(cueId) ?? []), item]);
  }
  return subtitles.cues.flatMap((cue, index) => {
    const takes = cue.id ? byCue.get(cue.id) : undefined;
    if (!takes?.length) return [];
    return [{ cueId: cue.id!, index, text: cue.text, takes: [...takes].sort((a, b) => b.createdAt.localeCompare(a.createdAt)) }];
  });
}

export interface TakeListProps {
  subtitles: SubtitleDocument;
  /** 当前作品的生成记录；只挑有 cueId 的逐句 take。 */
  generations: Generation[];
  /** 页面是否可见；切到其他页时停止试听。 */
  isActive: boolean;
  onError?: (message: string) => void;
}

// 音频库的逐句 take：按 cue 列出每次生成，选中一条并试听；这里只读历史，不改生成结果。
export function TakeList({ subtitles, generations, isActive, onError }: TakeListProps) {
  const locale = useLocale();
  const groups = cueTakes(subtitles, generations);
  const total = groups.reduce((count, group) => count + group.takes.length, 0);
  const audio = useRef<HTMLAudioElement>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);
  // 音频地址只认当前选中的 take；切换时旧的地址一并释放。
  const [source, setSource] = useState({ id: '', url: '' });
  const selectedFile = groups.flatMap(group => group.takes).find(item => item.id === selectedId)?.fileName ?? '';
  const url = source.id === selectedId ? source.url : '';
  // 点了试听但音频还没读好时记下那条 take，地址就绪且仍选中它时再开始。
  const pending = useRef<string | null>(null);

  useEffect(() => {
    if (!selectedId || !selectedFile) return;
    const id = selectedId;
    const element = audio.current;
    let disposed = false;
    let resource = '';
    mediaUrl('outputs', selectedFile).then(value => {
      resource = value;
      if (disposed) releaseMediaUrl(value);
      else setSource({ id, url: value });
    }, () => undefined);
    return () => {
      disposed = true;
      element?.pause();
      releaseMediaUrl(resource);
      setSource(current => current.id === id ? { id: '', url: '' } : current);
    };
  }, [selectedId, selectedFile]);

  // 页面切走时停止试听，避免阅读页隐藏后还在响。
  useEffect(() => {
    if (isActive) return;
    pending.current = null;
    audio.current?.pause();
  }, [isActive]);

  // 只选中不试听：丢掉刚才还没读好的试听请求，免得选中另一条时突然出声。
  function select(id: string) {
    pending.current = null;
    setSelectedId(id);
  }
  function play(take: Generation) {
    const element = audio.current;
    if (!element) { setSelectedId(take.id); return; }
    if (selectedId !== take.id) {
      setSelectedId(take.id);
      pending.current = take.id;
      return;
    }
    if (!url) { pending.current = take.id; return; }
    if (playingId === take.id) { element.pause(); return; }
    element.currentTime = 0;
    void element.play().catch(() => { setPlayingId(null); onError?.('@yovoice.error.audioPlayFailed'); });
  }
  // 地址就绪后接着播下用户点的那一条。
  useEffect(() => {
    const element = audio.current;
    if (pending.current !== selectedId || !url || !element || !isActive) return;
    pending.current = null;
    element.currentTime = 0;
    void element.play().catch(() => { setPlayingId(null); onError?.('@yovoice.error.audioPlayFailed'); });
  }, [selectedId, url, isActive, onError]);

  return <VStack as="section" className="audiobook-takes" gap={0} aria-label="音频库 take">
    <audio hidden ref={audio} src={url || undefined} preload="metadata"
      onPlay={() => setPlayingId(selectedId)} onPause={() => setPlayingId(null)} onEnded={() => setPlayingId(null)} />
    <HStack className="audiobook-takes-head" gap={2} vAlign="center">
      <Text type="label">音频库</Text>
      <Text type="supporting">逐句 take · {total} 条</Text>
      <Text className="audiobook-takes-hint" type="supporting">选中一条后可试听</Text>
    </HStack>
    {groups.length ? <VStack className="audiobook-take-cues" gap={0}>
      {groups.map(group => <VStack key={group.cueId} className="audiobook-take-group" gap={0}>
        <HStack className="audiobook-take-cue" gap={2} vAlign="center">
          <Text type="code" hasTabularNumbers>{group.index + 1}</Text>
          <Text className="audiobook-take-cue-id" type="code">{group.cueId}</Text>
          <Text className="audiobook-take-cue-text" type="supporting">{group.text}</Text>
        </HStack>
        <VStack as="ul" className="audiobook-take-rows" gap={0}>
          {group.takes.map((take, order) => {
            const selected = take.id === selectedId;
            const playing = take.id === playingId;
            return <HStack key={take.id} className="audiobook-take" data-take-id={take.id} data-cue-id={group.cueId}
              data-selected={selected ? 'true' : undefined} gap={2} vAlign="center">
              <Button size="sm" variant={selected ? 'secondary' : 'ghost'} isIconOnly icon={selected ? <Check /> : <Circle />}
                label={`选择第 ${group.index + 1} 句的第 ${order + 1} 条 take`} aria-pressed={selected} onClick={() => select(take.id)} />
              <Text className="audiobook-take-model" type="code">{take.settings.modelId}</Text>
              <Text className="audiobook-take-time" type="supporting">{formatDateTime(take.createdAt, locale, 'brief')}</Text>
              <Text className="audiobook-take-duration" type="code" hasTabularNumbers>{formatTime(take.duration)}</Text>
              <Button size="sm" variant="ghost" isIconOnly icon={playing ? <Square /> : <Play />}
                label={playing ? `停止试听第 ${group.index + 1} 句的第 ${order + 1} 条 take` : `试听第 ${group.index + 1} 句的第 ${order + 1} 条 take`}
                onClick={() => play(take)} />
            </HStack>;
          })}
        </VStack>
      </VStack>)}
    </VStack> : <EmptyState isCompact title="还没有逐句 take" description="生成逐句音频后，每次生成都会按 cue 保留在这里。" />}
  </VStack>;
}
