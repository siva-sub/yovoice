import { EmptyState } from '@astryxdesign/core/EmptyState';
import { HStack, Layout, LayoutContent, LayoutHeader, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { formatTime, type Character, type Draft, type QARecord, type Voice } from '../../shared/workbench';
import { qaVerdictLabels } from './shelf-model';

export interface CharacterPageProps {
  characters: Character[];
  voices: Voice[];
  /** 章节作品：按字幕里的 characterId 找该角色相关的章节验收记录。 */
  drafts: Draft[];
}

interface ChapterAudit { title: string; verdict: Exclude<QARecord['verdict'], 'unchecked'> }

// 锚点超过这个长度时，逐行渲染会静默退回 eager（ADR-0003 更正：RTF 从 0.44 变到约 1.9）。
const anchorEagerSeconds = 10;

// 角色页（ADR-0006/0008）：角色卡、锚点候选与别名。状态里没有的字段显示为空缺，不用示例数据补齐。
export function CharacterPage({ characters, voices, drafts }: CharacterPageProps) {
  return <Layout className="audiobook-cast-page" height="fill" padding={0}
    header={<LayoutHeader hasDivider padding={4}>
      <VStack className="audiobook-cast-header" gap={2}>
        <Heading level={1}>角色</Heading>
        <Text type="supporting">design 与锚点来自现有状态；f0、锚点审计与候选数组尚无后端字段时显示为空缺。</Text>
      </VStack>
    </LayoutHeader>}
    content={<LayoutContent padding={0} label="角色列表" role="region">
      <VStack className="audiobook-cast-content" gap={5} data-testid="audiobook-cast-page">
        {characters.length ? characters.map(character => <CastCard key={character.id} character={character} voices={voices} drafts={drafts} />)
          : <EmptyState title="还没有角色" description="在故事配音或我的声音里创建角色后，角色卡会显示在这里。" />}
        <VStack className="audiobook-alias" gap={2} data-testid="audiobook-alias">
          <HStack gap={2} vAlign="center" wrap="wrap">
            <Heading level={2}>别名与未知专名</Heading>
            <Text type="supporting">ADR-0008</Text>
          </HStack>
          <EmptyState title="还没有别名与专名数据" description="别名、亲属关系与未知专名需要 alias-graph.py 的输出；状态里目前没有这些字段，因此这里不展示示例数据。" />
        </VStack>
      </VStack>
    </LayoutContent>} />;
}

function CastCard({ character, voices, drafts }: { character: Character; voices: Voice[]; drafts: Draft[] }) {
  const voiceIds = new Set([character.settings.voiceId, ...(character.performances ?? []).map(item => item.settings.voiceId)].filter((id): id is string => !!id));
  const voice = voices.find(item => voiceIds.has(item.id));
  const design = character.settings.voiceDescription?.trim() ?? '';
  const audits = chapterAudits(character, drafts);
  const preview = character.preview;
  const anchorTooLong = !!voice && voice.duration > anchorEagerSeconds;
  return <VStack className="audiobook-cast-card" gap={3} data-testid="audiobook-cast-card" data-character-id={character.id}>
    <HStack className="audiobook-cast-card-head" gap={3} vAlign="center" wrap="wrap">
      <Heading level={2}>{character.name}</Heading>
      <Text type="supporting" data-testid="audiobook-cast-anchor">{voice ? `锚点：已绑定 · ${voice.name}` : '锚点：未绑定声音'}</Text>
    </HStack>
    <VStack className="audiobook-cast-design" gap={1}>
      <Text type="label">design</Text>
      <Text data-testid="audiobook-cast-design">{design || '未填写声音描述'}</Text>
    </VStack>
    <HStack className="audiobook-cast-facts" gap={4} wrap="wrap">
      <Text type="supporting">f0：未记录</Text>
      <Text type="supporting" data-testid="audiobook-cast-audit">{audits.length ? `审计：${audits.map(item => `${item.title} 章节验收${qaVerdictLabels[item.verdict]}`).join(' · ')}` : '审计：未审计'}</Text>
    </HStack>
    <VStack className="audiobook-candidates" gap={2}>
      <HStack gap={2} vAlign="center" wrap="wrap">
        <Text type="label">锚点候选</Text>
        <Text type="supporting">状态里没有候选数组：这里只放当前锚点，其余空位需要后端支持。</Text>
      </HStack>
      <HStack className="audiobook-candidate-grid" gap={3} data-testid="audiobook-candidate-grid">
        <VStack className="audiobook-candidate" gap={1}>
          {preview ? <>
            <Text type="label">当前预览</Text>
            <Text type="supporting">{preview.settings.modelId}</Text>
            <Text type="code" hasTabularNumbers>{formatTime(preview.duration)}</Text>
          </> : voice ? <>
            <Text type="label">{voice.name}</Text>
            <Text type="supporting">当前锚点音频</Text>
            <Text type="code" hasTabularNumbers>{formatTime(voice.duration)}</Text>
          </> : <Text type="supporting">还没有候选</Text>}
        </VStack>
        {['空位 2', '空位 3'].map(slot => <VStack key={slot} className="audiobook-candidate audiobook-candidate-empty" gap={1}>
          <Text type="label">{slot}</Text>
          <Text type="supporting">需要后端的候选数组</Text>
        </VStack>)}
      </HStack>
      {anchorTooLong ? <Text className="audiobook-anchor-warning" type="supporting" role="status" data-testid="audiobook-anchor-warning">
        锚点 {formatTime(voice.duration)}，超过约 {anchorEagerSeconds} 秒：逐行渲染会静默退回 eager，渲染成本约 4 倍（RTF 0.44 → 1.9）。
      </Text> : null}
    </VStack>
  </VStack>;
}

/** 角色在字幕里被引用、且该章已有通过或不通过的验收时，列出这些章节验收；角色自身的审计字段仍为空。 */
function chapterAudits(character: Character, drafts: Draft[]): ChapterAudit[] {
  return drafts.flatMap(draft => {
    const verdict = draft.qa?.verdict;
    if (verdict !== 'pass' && verdict !== 'fail') return [];
    return draft.subtitles?.speakers.some(speaker => speaker.characterId === character.id) ? [{ title: draft.title, verdict }] : [];
  });
}
