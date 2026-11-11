import { EmptyState } from '@astryxdesign/core/EmptyState';
import { VStack } from '@astryxdesign/core/Layout';
import type { Gate, Generation, SubtitleDocument } from '../../shared/workbench';
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
}

// 归因阅读页：上半是整章阅读与改派，下半是当前作品的逐句 take。
export function ReadingPage({ chapterId, title, subtitles, gate, generations, isActive, onCueChange, onError }: ReadingPageProps) {
  if (!subtitles) return <VStack className="audiobook-empty" hAlign="center" vAlign="center" gap={0}>
    <EmptyState title="还没有可阅读的章节" description="导入带说话人的书稿后，归因阅读会显示在这里。" />
  </VStack>;
  return <VStack className="audiobook-page" gap={0}>
    <VStack className="audiobook-page-reading" gap={0}>
      <AudiobookReadingView chapterId={chapterId} title={title} subtitles={subtitles} gate={gate} isActive={isActive} onCueChange={onCueChange} />
    </VStack>
    <TakeList subtitles={subtitles} generations={generations} isActive={isActive} onError={onError} />
  </VStack>;
}
