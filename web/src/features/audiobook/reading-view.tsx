import { useEffect, useEffectEvent, useRef, useState, type ReactNode } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { HStack, Layout, LayoutContent, LayoutHeader, VStack } from '@astryxdesign/core/Layout';
import { Kbd } from '@astryxdesign/core/Kbd';
import { SegmentedControl, SegmentedControlItem } from '@astryxdesign/core/SegmentedControl';
import { Tooltip } from '@astryxdesign/core/Tooltip';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Keyboard, Play, Square, Undo2, X } from 'lucide-react';
import { readLocal, writeLocal } from '../../shared/lib/local-store';
import type { Gate, SubtitleCue, SubtitleDocument } from '../../shared/workbench';
import {
  cueKind, evidenceLabel, evidenceMark, kindLabels, kindMarks, reviewable, reviewQueue, reviewTarget,
  speakerName, speakerKeys,
} from './reading-model';
import { gateLabels } from './shelf-model';
import { usePrompter } from './prompter';

/** 一行的人工决定：改派或接受时 confidence 为 undefined（写回后删除该字段），撤销时恢复为原值。 */
export interface CueReassignment {
  index: number;
  cueId?: string;
  speakerId: string;
  confidence?: number;
}

export interface AudiobookReadingViewProps {
  /** 章节标识；换章节时撤销栈、键位映射与选中行一起重置。 */
  chapterId: string;
  subtitles: SubtitleDocument;
  title?: string;
  /** 章节闸门状态，只用于页眉显示；不传时按待读显示。 */
  gate?: Gate;
  /** 页头的闸门动作；阅读页传入，单独挂载阅读视图时不显示。 */
  gateActions?: ReactNode;
  /** 每次改派、接受或撤销后回调；接入时写回 draft.subtitles。 */
  onCueChange?: (change: CueReassignment) => void;
  /** 提词器播放一句的接口；接入音频后在这里播放，未提供时只走高亮。 */
  playCue?: (cue: SubtitleCue, index: number) => void;
  /** 页面是否可见；接入导航后切到其他页时全局键位不再改派，默认可见。 */
  isActive?: boolean;
}

// 归因阅读页：一行一句，序号、类型、说话人、正文四栏；改派与复核都按整章持有状态。
export function AudiobookReadingView(props: AudiobookReadingViewProps) {
  // 用 chapterId 作 key 重新挂载：撤销栈与 1–9 的说话人映射只在章节内有效，离开章节即丢弃。
  return <ReadingChapter key={props.chapterId} {...props} />;
}

interface Assignment { speakerId: string; confidence?: number }
interface UndoEntry extends Assignment { index: number }

function ReadingChapter({ subtitles, title, gate, gateActions, onCueChange, playCue, isActive = true }: AudiobookReadingViewProps) {
  const cues = subtitles.cues;
  // 键位在进入章节时按首次出场顺序固定，之后改派不重排。
  const [cast] = useState(() => speakerKeys(subtitles.speakers, cues));
  const [overrides, setOverrides] = useState<Record<number, Assignment>>({});
  const [active, setActive] = useState<number | null>(cues.length ? 0 : null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const undoStack = useRef<UndoEntry[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prompter = usePrompter(cues, playCue);
  const [density, setDensity] = useState<'compact' | 'comfortable'>(() => readLocal('yovoice-reading-density') === 'compact' ? 'compact' : 'comfortable');
  const [helpOpen, setHelpOpen] = useState(false);
  const shownGate = gate?.status ?? 'pending';
  function changeDensity(value: string) {
    const next = value === 'compact' ? 'compact' : 'comfortable';
    setDensity(next);
    writeLocal('yovoice-reading-density', next);
  }

  const currentCues = cues.map((cue, index) => {
    const assignment = overrides[index];
    return assignment ? { ...cue, speakerId: assignment.speakerId, confidence: assignment.confidence } : cue;
  });
  const queue = reviewQueue(currentCues);
  const queuePosition = active === null ? 0 : queue.indexOf(active) + 1;

  function speakerAt(index: number) {
    return overrides[index] ? overrides[index].speakerId : cues[index].speakerId;
  }
  function confidenceAt(index: number) {
    return overrides[index] ? overrides[index].confidence : cues[index].confidence;
  }
  // 所有键盘改动走这里：先记下旧值供撤销，再写本地状态并通知外部保存。
  function commit(index: number, next: Assignment) {
    undoStack.current.push({ index, speakerId: speakerAt(index), confidence: confidenceAt(index) });
    if (undoStack.current.length > 200) undoStack.current.shift();
    setOverrides(current => ({ ...current, [index]: next }));
    setActive(index);
    onCueChange?.({ index, cueId: cues[index].id, speakerId: next.speakerId, confidence: next.confidence });
  }
  function assignSpeaker(speakerId: string) {
    if (active === null) return;
    commit(active, { speakerId, confidence: undefined });
  }
  function accept() {
    if (active === null || confidenceAt(active) === undefined) return;
    commit(active, { speakerId: speakerAt(active), confidence: undefined });
  }
  function undo() {
    const entry = undoStack.current.pop();
    if (!entry) return;
    const { index, ...assignment } = entry;
    setOverrides(current => ({ ...current, [index]: assignment }));
    setActive(index);
    onCueChange?.({ index, cueId: cues[index].id, speakerId: assignment.speakerId, confidence: assignment.confidence });
  }

  // 快捷键在页面任意位置生效；页面被导航切走时让给当前页面。焦点在输入框里时让给输入框自身的编辑操作。
  const onKey = useEffectEvent((event: KeyboardEvent) => {
    if (!isActive) return;
    const target = event.target as HTMLElement | null;
    if (target?.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]')) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); undo(); return; }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const key = event.key.toLowerCase();
    if (key.length === 1 && key >= '0' && key <= '9') {
      if (key === '0') { event.preventDefault(); assignSpeaker(''); return; }
      const speaker = cast.find(item => item.key === key);
      if (speaker) { event.preventDefault(); assignSpeaker(speaker.speakerId); }
      return;
    }
    if (key === 'j' || key === 'k') {
      event.preventDefault();
      const next = reviewTarget(currentCues, active, key === 'j' ? 1 : -1);
      if (next !== null) setActive(next);
      return;
    }
    if (key === 'a') { event.preventDefault(); accept(); }
  });
  useEffect(() => {
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // 当前行（含 J/K 与提词器推进）保持在可视区内。
  useEffect(() => {
    if (active === null) return;
    scrollRef.current?.querySelector(`[data-cue-index="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [active]);
  useEffect(() => {
    if (prompter.index === null) return;
    scrollRef.current?.querySelector(`[data-cue-index="${prompter.index}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [prompter.index]);

  function toggleExpanded(index: number) {
    setExpanded(current => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index); else next.add(index);
      return next;
    });
  }

  const header = <LayoutHeader hasDivider padding={3}>
    <VStack className="audiobook-header" gap={2}>
      <HStack className="audiobook-toolbar" gap={3} vAlign="center" wrap="wrap">
        <Heading level={1} className="audiobook-title">{title ?? '归因阅读'}</Heading>
        <HStack className="audiobook-gate" gap={2} vAlign="center" wrap="wrap" data-testid="audiobook-gate">
          <Text type="supporting" data-testid="audiobook-gate-status">闸门：{gateLabels[shownGate]}</Text>
          {gateActions}
        </HStack>
        <HStack className="audiobook-review" gap={1} vAlign="center" role="status" data-testid="audiobook-review-status">
          <Text type="supporting">复核</Text>
          <Text type="code" hasTabularNumbers>第 {queuePosition}/{queue.length} 处</Text>
        </HStack>
        <Button size="sm" variant="secondary" icon={<Undo2 />} label="撤销" tooltip="撤销上一次改派（Ctrl+Z）" isDisabled={undoStack.current.length === 0} onClick={undo} />
        <Button size="sm" variant="secondary" icon={prompter.playing ? <Square /> : <Play />} label={prompter.playing ? '停止试听' : '提词器试听'} onClick={prompter.toggle} />
        <SegmentedControl className="audiobook-density" label="行密度" size="sm" value={density} onChange={changeDensity}>
          <SegmentedControlItem value="compact" label="紧凑" />
          <SegmentedControlItem value="comfortable" label="舒适" />
        </SegmentedControl>
        <Button size="sm" variant="secondary" icon={<Keyboard />} label="键盘帮助" data-testid="audiobook-keyboard-help-toggle"
          aria-expanded={helpOpen} onClick={() => setHelpOpen(open => !open)} />
      </HStack>
      <HStack className="audiobook-cast" gap={3} vAlign="center" wrap="wrap">
        <Text type="supporting">说话人</Text>
        {cast.map(item => <HStack key={item.speakerId} className="audiobook-cast-item" gap={1} vAlign="center">
          <Kbd keys={item.key} />
          <Text type="label">{item.name}</Text>
        </HStack>)}
        <HStack className="audiobook-cast-item" gap={1} vAlign="center"><Kbd keys="0" /><Text type="label">旁白</Text></HStack>
        <Text className="audiobook-help" type="supporting">J/K 复核 · A 接受 · Ctrl+Z 撤销</Text>
      </HStack>
      {helpOpen ? <VStack className="audiobook-keyboard-help" gap={2} role="region" aria-label="键盘帮助" data-testid="audiobook-keyboard-help">
        <HStack className="audiobook-keyboard-head" hAlign="between" vAlign="center" gap={2}>
          <Text type="label">键盘帮助</Text>
          <Button size="sm" variant="ghost" isIconOnly icon={<X />} label="关闭键盘帮助" data-testid="audiobook-keyboard-help-close" onClick={() => setHelpOpen(false)} />
        </HStack>
        <HStack className="audiobook-keyboard-items" gap={3} wrap="wrap">
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="1" /><Text type="supporting">–</Text><Kbd keys="9" /><Text type="label">改派给对应说话人</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="0" /><Text type="label">改派给旁白</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="j" /><Text type="label">下一处复核</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="k" /><Text type="label">上一处复核</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="a" /><Text type="label">接受当前归因</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="mod+z" /><Text type="label">撤销上一次改派</Text></HStack>
        </HStack>
      </VStack> : null}
      {prompter.index !== null ? <Text className="audiobook-prompting" type="supporting" role="status">
        正在试听：第 {prompter.index + 1} 句 · {speakerName(subtitles.speakers, currentCues[prompter.index]?.speakerId ?? '')}
      </Text> : null}
    </VStack>
  </LayoutHeader>;

  const content = <LayoutContent ref={scrollRef} padding={0} label="章节正文" role="region">
    {cues.length ? <VStack as="ol" className="audiobook-cues" gap={0} data-density={density} data-testid="audiobook-cues">
      {cues.map((cue, index) => {
        const assignment = overrides[index];
        const speakerId = assignment ? assignment.speakerId : cue.speakerId;
        const confidence = assignment ? assignment.confidence : cue.confidence;
        const name = speakerName(subtitles.speakers, speakerId);
        const kind = cueKind(cue);
        const key = cast.find(item => item.speakerId === speakerId)?.key;
        const low = reviewable(confidence);
        const isExpanded = expanded.has(index);
        return <HStack as="li" key={cue.id ?? `cue-${index}`} className="audiobook-cue" data-cue-index={index} data-cue-id={cue.id}
          data-confidence={confidence === undefined ? undefined : confidence} data-low-confidence={low ? 'true' : undefined}
          data-prompting={prompter.index === index ? 'true' : undefined} aria-current={active === index ? 'true' : undefined}
          onClick={() => setActive(index)} gap={0} vAlign="start">
          <Text className="audiobook-index" type="code" hasTabularNumbers>{index + 1}</Text>
          <Tooltip content={kindLabels[kind]} focusTrigger="never"><Text className="audiobook-kind" type="code">{kindMarks[kind]}</Text></Tooltip>
          <VStack className="audiobook-speaker" gap={0}>
            <Text type="label">{name}</Text>
            {key ? <Text className="audiobook-speaker-key" type="code">{key}</Text> : null}
          </VStack>
          <HStack className="audiobook-line" gap={2} vAlign="start">
            <button type="button" className="audiobook-text" data-expanded={isExpanded ? 'true' : 'false'} aria-expanded={isExpanded}
              aria-label={`第 ${index + 1} 句，${kindLabels[kind]}，${name}`}
              onClick={event => { event.stopPropagation(); setActive(index); toggleExpanded(index); }}>
              <Text as="span" textWrap="pretty">{cue.text}</Text>
            </button>
            {confidence !== undefined ? <Tooltip content={evidenceLabel(cue.evidence)} focusTrigger="never">
              <Text className="audiobook-evidence" type="code">{evidenceMark(name, confidence, cue.evidence)}</Text>
            </Tooltip> : null}
          </HStack>
        </HStack>;
      })}
    </VStack> : <EmptyState title="本章还没有句子" description="导入或生成台词后，归因会显示在这里。" />}
  </LayoutContent>;

  return <Layout className="audiobook-reading" height="fill" padding={0} header={header} content={content} />;
}
