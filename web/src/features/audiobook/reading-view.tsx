import { useEffect, useEffectEvent, useRef, useState, type ReactNode } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { HStack, Layout, LayoutContent, LayoutHeader, VStack } from '@astryxdesign/core/Layout';
import { Kbd } from '@astryxdesign/core/Kbd';
import { SegmentedControl, SegmentedControlItem } from '@astryxdesign/core/SegmentedControl';
import { Tooltip } from '@astryxdesign/core/Tooltip';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Keyboard, Play, Square, Undo2, X } from 'lucide-react';
import { call } from '../../shared/lib/client';
import { readLocal, writeLocal } from '../../shared/lib/local-store';
import type { Gate, SubtitleCue, SubtitleDocument } from '../../shared/workbench';
import {
  cueKind, evidenceLabel, evidenceMark, kindKeys, kindLabels, kindMarks, reviewable, reviewQueue, reviewTarget,
  speakerName, speakerKeys,
} from './reading-model';
import type { CueKind } from './reading-model';
import { gateLabels } from './shelf-model';
import { usePrompter } from './prompter';

/** 一行的人工决定：改派或接受时 confidence 为 undefined（写回后删除该字段），撤销时恢复为原值。 */
export interface CueReassignment {
  index: number;
  cueId?: string;
  speakerId: string;
  /** 只改类型时带上；不传表示类型不变。 */
  kind?: CueKind;
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
  /** 批量改派一次写回；不提供时逐条调用 onCueChange。 */
  onCuesChange?: (changes: CueReassignment[]) => void;
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

interface Assignment { speakerId: string; kind?: CueKind; confidence?: number }
interface UndoEntry extends Assignment { index: number }

/** 批量改派失败时的提示：本地已经逐条改过，不能装作什么都没发生。 */
const batchFallbackNotice = '批量接口未接，已逐条修改';

function ReadingChapter({ chapterId, subtitles, title, gate, gateActions, onCueChange, onCuesChange, playCue, isActive = true }: AudiobookReadingViewProps) {
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
  // 选区模式：V 进入，J/K 移动末端扩展（anchor 不动，逆序时选区自动取两者之间），回车弹出动作条。
  const [selection, setSelection] = useState<{ anchor: number; end: number } | null>(null);
  const [selectionActions, setSelectionActions] = useState(false);
  const [batchNotice, setBatchNotice] = useState('');
  const shownGate = gate?.status ?? 'pending';
  function changeDensity(value: string) {
    const next = value === 'compact' ? 'compact' : 'comfortable';
    setDensity(next);
    writeLocal('yovoice-reading-density', next);
  }

  const currentCues = cues.map((cue, index) => {
    const assignment = overrides[index];
    return assignment ? { ...cue, speakerId: assignment.speakerId, kind: assignment.kind ?? cue.kind, confidence: assignment.confidence } : cue;
  });
  const queue = reviewQueue(currentCues);
  const queuePosition = active === null ? 0 : queue.indexOf(active) + 1;
  const selectedRows = new Set(selection ? Array.from({ length: Math.abs(selection.end - selection.anchor) + 1 }, (_, offset) => Math.min(selection.anchor, selection.end) + offset) : []);

  function speakerAt(index: number) {
    return overrides[index] ? overrides[index].speakerId : cues[index].speakerId;
  }
  function confidenceAt(index: number) {
    return overrides[index] ? overrides[index].confidence : cues[index].confidence;
  }
  // 撤销要恢复原来的类型；没有人工改过时按正文推断，与界面显示的一致。
  function kindAt(index: number): CueKind {
    return overrides[index]?.kind ?? cueKind(cues[index]);
  }
  // 所有键盘改动走这里：先记下旧值供撤销，再写本地状态并通知外部保存。
  function commit(index: number, next: Assignment) {
    undoStack.current.push({ index, speakerId: speakerAt(index), kind: kindAt(index), confidence: confidenceAt(index) });
    if (undoStack.current.length > 200) undoStack.current.shift();
    setOverrides(current => ({ ...current, [index]: next }));
    setActive(index);
    onCueChange?.({ index, cueId: cues[index].id, speakerId: next.speakerId, kind: next.kind, confidence: next.confidence });
  }
  function assignSpeaker(speakerId: string) {
    if (active === null) return;
    commit(active, { speakerId, kind: overrides[active]?.kind, confidence: undefined });
  }
  // 改类型不清空说话人（旧流程的覆盖键也把 kind 与 speaker 分开记）；与改派一样清空置信度。
  function assignKind(kind: CueKind) {
    if (active === null) return;
    commit(active, { speakerId: speakerAt(active), kind, confidence: undefined });
  }
  function accept() {
    if (active === null || confidenceAt(active) === undefined) return;
    commit(active, { speakerId: speakerAt(active), kind: overrides[active]?.kind, confidence: undefined });
  }
  function undo() {
    const entry = undoStack.current.pop();
    if (!entry) return;
    const { index, ...assignment } = entry;
    setOverrides(current => ({ ...current, [index]: assignment }));
    setActive(index);
    onCueChange?.({ index, cueId: cues[index].id, speakerId: assignment.speakerId, kind: assignment.kind, confidence: assignment.confidence });
  }

  function toggleSelection() {
    setBatchNotice('');
    if (selection) { setSelection(null); setSelectionActions(false); return; }
    if (!cues.length) return;
    const anchor = active ?? 0;
    setActive(anchor);
    setSelection({ anchor, end: anchor });
  }
  function extendSelection(step: 1 | -1) {
    if (!selection) return;
    const end = Math.min(cues.length - 1, Math.max(0, selection.end + step));
    setSelection({ ...selection, end });
    setActive(end);
  }
  function closeSelection() {
    if (selectionActions) { setSelectionActions(false); return; }
    setSelection(null);
  }
  // 批量改派：先记撤销、写本地并通知外部，再尝试批量接口；接口不存在时逐条修改已经生效，只补一条提示。
  async function applySelection(assignment: { speakerId?: string; kind?: CueKind }) {
    if (!selection) return;
    const indexes = [...selectedRows];
    if (!indexes.length) return;
    const changes = indexes.map((index): CueReassignment => ({
      index,
      cueId: cues[index].id,
      speakerId: assignment.speakerId ?? speakerAt(index),
      // 只改说话人时保留之前的人工类型决定，不能退回原文推断。
      kind: assignment.kind ?? overrides[index]?.kind,
      confidence: undefined,
    }));
    for (const change of changes) undoStack.current.push({ index: change.index, speakerId: speakerAt(change.index), kind: kindAt(change.index), confidence: confidenceAt(change.index) });
    if (undoStack.current.length > 200) undoStack.current.splice(0, undoStack.current.length - 200);
    setOverrides(current => {
      const next = { ...current };
      for (const change of changes) next[change.index] = { speakerId: change.speakerId, kind: change.kind, confidence: undefined };
      return next;
    });
    setActive(indexes[indexes.length - 1]);
    if (onCuesChange) onCuesChange(changes); else for (const change of changes) onCueChange?.(change);
    setSelection(null);
    setSelectionActions(false);
    // 没有 cue id 的旧数据无法交给批量接口，按 RPC 不可用处理。
    const cueIds = changes.map(change => change.cueId);
    if (cueIds.some(id => !id)) { setBatchNotice(batchFallbackNotice); return; }
    try {
      await call('draft.reassignCues', { id: chapterId, cueIds, speakerId: assignment.speakerId ?? '', kind: assignment.kind ?? '' });
      setBatchNotice('');
    } catch {
      setBatchNotice(batchFallbackNotice);
    }
  }

  // 快捷键在页面任意位置生效；页面被导航切走时让给当前页面。焦点在输入框里时让给输入框自身的编辑操作。
  const onKey = useEffectEvent((event: KeyboardEvent) => {
    if (!isActive) return;
    const target = event.target as HTMLElement | null;
    if (target?.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]')) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); undo(); return; }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const key = event.key.toLowerCase();
    // 选区模式先接管 J/K/回车/Esc 与动作条里的单键，避免扩展选区时误改当前行。
    if (key === 'escape') {
      if (!selection) return;
      event.preventDefault();
      closeSelection();
      return;
    }
    if (key === 'v') { event.preventDefault(); toggleSelection(); return; }
    if (selection) {
      if (key === 'j' || key === 'k') { event.preventDefault(); extendSelection(key === 'j' ? 1 : -1); return; }
      if (key === 'enter') { event.preventDefault(); setBatchNotice(''); setSelectionActions(true); return; }
      if (selectionActions) {
        if (key === '0') { event.preventDefault(); void applySelection({ speakerId: '' }); return; }
        if (key.length === 1 && key >= '1' && key <= '9') {
          const speaker = cast.find(item => item.key === key);
          if (speaker) { event.preventDefault(); void applySelection({ speakerId: speaker.speakerId }); }
          return;
        }
        const kind = kindKeys[key as keyof typeof kindKeys];
        if (kind) { event.preventDefault(); void applySelection({ kind }); }
      }
      return;
    }
    if (key.length === 1 && key >= '0' && key <= '9') {
      if (key === '0') { event.preventDefault(); assignSpeaker(''); return; }
      const speaker = cast.find(item => item.key === key);
      if (speaker) { event.preventDefault(); assignSpeaker(speaker.speakerId); }
      return;
    }
    if (key === 'd' || key === 'n' || key === 't') { event.preventDefault(); assignKind(kindKeys[key]); return; }
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
        <Text className="audiobook-help" type="supporting">J/K 复核 · A 接受 · Ctrl+Z 撤销 · D/N/T 类型 · V 选区</Text>
      </HStack>
      {selection ? <HStack className="audiobook-selection" gap={2} vAlign="center" wrap="wrap" role="status" data-testid="audiobook-selection">
        <Text type="label">已选 {selectedRows.size} 句</Text>
        <Text type="supporting">第 {Math.min(selection.anchor, selection.end) + 1}–{Math.max(selection.anchor, selection.end) + 1} 句 · J/K 扩展 · {selectionActions ? '选说话人或类型' : '回车选择动作'} · Esc 取消</Text>
        {selectionActions ? <HStack className="audiobook-selection-actions" gap={1} vAlign="center" wrap="wrap" role="toolbar" aria-label="批量改派" data-testid="audiobook-selection-bar">
          {cast.map(item => <Button key={item.speakerId} size="sm" variant="secondary" label={`设为 ${item.name}`} onClick={() => void applySelection({ speakerId: item.speakerId })} />)}
          <Button size="sm" variant="secondary" label="设为旁白" onClick={() => void applySelection({ speakerId: '' })} />
          {(['dialogue', 'narration', 'thought'] as const).map(kind => <Button key={kind} size="sm" variant="secondary" label={`设为${kindLabels[kind]}`} onClick={() => void applySelection({ kind })} />)}
        </HStack> : null}
      </HStack> : null}
      {batchNotice ? <Text className="audiobook-batch-notice" type="supporting" role="status" data-testid="audiobook-batch-notice">{batchNotice}</Text> : null}
      {helpOpen ? <VStack className="audiobook-keyboard-help" gap={2} role="region" aria-label="键盘帮助" data-testid="audiobook-keyboard-help">
        <HStack className="audiobook-keyboard-head" hAlign="between" vAlign="center" gap={2}>
          <Text type="label">键盘帮助</Text>
          <Button size="sm" variant="ghost" isIconOnly icon={<X />} label="关闭键盘帮助" data-testid="audiobook-keyboard-help-close" onClick={() => setHelpOpen(false)} />
        </HStack>
        <HStack className="audiobook-keyboard-items" gap={3} wrap="wrap">
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="1" /><Text type="supporting">–</Text><Kbd keys="9" /><Text type="label">改派给对应说话人</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="0" /><Text type="label">改派给旁白</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="d" /><Text type="label">设为对白</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="n" /><Text type="label">设为叙述</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="t" /><Text type="label">设为心声</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="j" /><Text type="label">下一处复核</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="k" /><Text type="label">上一处复核</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="a" /><Text type="label">接受当前归因</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="v" /><Text type="label">进入或退出选区</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="j" /><Text type="supporting">/</Text><Kbd keys="k" /><Text type="label">选区内扩展选区</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="enter" /><Text type="label">选区动作条</Text></HStack>
          <HStack className="audiobook-keyboard-item" gap={1} vAlign="center"><Kbd keys="escape" /><Text type="label">关闭动作条或取消选区</Text></HStack>
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
        const kind = cueKind(currentCues[index]);
        const key = cast.find(item => item.speakerId === speakerId)?.key;
        const low = reviewable(confidence);
        const isExpanded = expanded.has(index);
        return <HStack as="li" key={cue.id ?? `cue-${index}`} className="audiobook-cue" data-cue-index={index} data-cue-id={cue.id}
          data-confidence={confidence === undefined ? undefined : confidence} data-low-confidence={low ? 'true' : undefined}
          data-selected={selectedRows.has(index) ? 'true' : undefined}
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
