import { useState } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { HStack } from '@astryxdesign/core/Layout';
import { Text } from '@astryxdesign/core/Text';
import { useTranslator } from '@astryxdesign/core/i18n';
import { call } from '../../shared/lib/client';
import { formatCallError } from '../../shared/i18n/format';
import type { Gate } from '../../shared/workbench';

/** 闸门动作只经过这两个入口；界面其余部分不直接拼 RPC 方法名。 */
export async function confirmChapterRead(id: string): Promise<void> {
  await call('chapter.confirmRead', { id });
}

export async function verifyChapter(id: string): Promise<void> {
  await call('chapter.verify', { id });
}

export interface ChapterGateActionsProps {
  chapterId: string;
  status: Gate['status'];
  /** 动作成功后先把页头切到新状态，等服务端推送或刷新返回后再以状态里的值为准。 */
  onStatus: (status: Gate['status']) => void;
  /** 动作成功后取回这一章的最新状态；失败不影响动作本身的结果。 */
  onRefresh?: (id: string) => Promise<void>;
}

// 阅读页页头的闸门动作：先确认已读，再确认校验。两个动作都由后端判定，界面只负责禁用与显示错误。
export function ChapterGateActions({ chapterId, status, onStatus, onRefresh }: ChapterGateActionsProps) {
  const t = useTranslator();
  const [busy, setBusy] = useState<'read' | 'verify' | null>(null);
  const [error, setError] = useState('');

  async function run(action: 'read' | 'verify') {
    setBusy(action);
    setError('');
    try {
      if (action === 'read') await confirmChapterRead(chapterId);
      else await verifyChapter(chapterId);
      onStatus(action === 'read' ? 'read' : 'verified');
      await onRefresh?.(chapterId).catch(() => undefined);
    } catch (cause) {
      setError(formatCallError(t, cause));
    } finally {
      setBusy(null);
    }
  }

  return <HStack className="audiobook-gate-actions" gap={2} vAlign="center" wrap="wrap" data-testid="audiobook-gate-actions">
    <Button size="sm" variant="secondary" label="确认已读" data-testid="audiobook-confirm-read"
      isDisabled={status !== 'pending'} isLoading={busy === 'read'} onClick={() => void run('read')} />
    <Button size="sm" variant="primary" label="确认校验" data-testid="audiobook-verify"
      isDisabled={status !== 'read' && status !== 'attributed'} isLoading={busy === 'verify'} onClick={() => void run('verify')} />
    {error ? <Text className="audiobook-gate-error" type="supporting" role="alert" data-testid="audiobook-gate-error">{error}</Text> : null}
  </HStack>;
}
