import { useEffect, useState } from 'react';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { VStack } from '@astryxdesign/core/Layout';
import type { Gate, Generation, SubtitleDocument } from '../../shared/workbench';
import { ChapterGateActions } from './chapter-gate';
import { AudiobookReadingView, type CueReassignment } from './reading-view';
import { TakeList } from './take-list';

export interface ReadingPageProps {
  chapterId: string;
  title: string;
  /** 当前作品的字幕；没有字幕时整页给空状态。 */
  subtitles?: SubtitleDocument;
  gate?: Gate;
  /** 当前作品的生成记录，take 区自己挑出逐句的。 */
  generations: Generation[];
  /** 页面是否可见；离开后阅读页的全局键位与试听都要停。 */
  isActive: boolean;
  onCueChange: (change: CueReassignment) => void;
  onError?: (message: string) => void;
  /** 闸门动作成功后取回这一章的最新状态；书架与阅读页共用。 */
  onRefreshChapter?: (id: string) => Promise<void>;
}

// 归因阅读页：上半是整章阅读与改派，下半是当前作品的逐句 take。
export function ReadingPage({ chapterId, title, subtitles, gate, generations, isActive, onCueChange, onError, onRefreshChapter }: ReadingPageProps) {
  // 动作成功后先显示新状态；状态推送到达或刷新返回后以 gate 为准。换章时清掉上一章的临时状态。
  const [status, setStatus] = useState<Gate['status'] | null>(null);
  useEffect(() => setStatus(gate?.status ?? null), [gate?.status, chapterId]);
  const shown = status ?? gate?.status ?? 'pending';
  if (!subtitles) return <VStack className="audiobook-empty" hAlign="center" vAlign="center" gap={0}>
    <EmptyState title="还没有可阅读的章节" description="导入带说话人的书稿后，归因阅读会显示在这里。" />
  </VStack>;
  return <VStack className="audiobook-page" gap={0}>
    <VStack className="audiobook-page-reading" gap={0}>
      <AudiobookReadingView chapterId={chapterId} title={title} subtitles={subtitles}
        gate={{ status: shown, textSha: gate?.textSha ?? '', verifiedAt: gate?.verifiedAt, note: gate?.note }}
        gateActions={<ChapterGateActions chapterId={chapterId} status={shown} onStatus={setStatus} onRefresh={onRefreshChapter} />}
        isActive={isActive} onCueChange={onCueChange} />
    </VStack>
    <TakeList subtitles={subtitles} generations={generations} isActive={isActive} onError={onError} />
  </VStack>;
}
