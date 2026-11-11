# 界面与助手接入契约（第二轮）

上一轮把能力做进了后端，但**界面够不着、助手也不知道**：`workbench` 有 `ConfirmRead` /
`VerifyChapter` / `RecordQA` / `PendingQueue`，但没有任何 RPC 暴露，前端只能显示空状态；
助手也没有一条能操作有声书流程的工具。这份契约定死名字，两条车道分开写。

## 一、RPC 方法（`internal/desktop/rpc.go` 的 switch）

| 方法 | 入参 | 返回 | 落到哪 |
| --- | --- | --- | --- |
| `chapter.confirmRead` | `{id string}` | `true` | `workbench.ConfirmRead` |
| `chapter.verify` | `{id string, note string}`（note 可选） | `true` | `workbench.VerifyChapter` |
| `chapter.reopen` | `{id string}` | `true` | `workbench.ReopenChapter` |
| `chapter.recordQA` | `{id string, qa QARecord}` | `true` | `workbench.RecordQA` |
| `queue.pending` | `{bookId string}`（空表示全部） | `[]QueueItem` | `workbench.PendingQueue` |

`QARecord` 与 `QueueItem` 用 Go 结构体直接编解码，JSON 字段名沿用
`docs/audiobook-contracts.md`（`wer`/`lufs`/`peak`/`silences`/`verdict`/`at`/`note`；
队列项 `kind`/`draftId`/`chapter`/`reason`）。

错误语义：闸门类错误（未读就校验、未验证就渲染）必须原样返回现有错误码，不要包成新码，
否则界面无法把错误映射到对应动作。

## 二、助手工具（`mcp.AddTool`，与现有 `registerAgentTools` 同风格）

| 工具名 | 作用 | 注意 |
| --- | --- | --- |
| `list_pending_chapters` | 返回书级待办（该读/该校验/该渲染/该验收） | 只读，按章升序 |
| `verify_chapter` | 确认已读并校验一章 | 会改变闸门状态；描述里写清副作用与不可逆性 |
| `reassign_cue` | 把一句改派给某说话人 | 必须同时清空该 cue 的 `confidence`（ADR-0002） |
| `record_qa` | 写入一章的验收结果 | `verdict=fail` 必须带 note |
| `check_mix` | 检查混音电平/侧链/提前量 | 只读，返回问题列表 |

描述用中文、与现有工具同等详尽（写清「什么时候该调用」），因为助手是靠描述选工具的。

## 三、前端要接的东西

- 书架“下一件事”的按钮必须真的能执行：`confirmRead` / `verify` / 跳到作品
- 阅读页页头显示闸门状态，并提供「确认已读并校验」入口
- 助手面板在书架/阅读页给对应的快捷动作（不是通用建议）

## 四、边界

| 车道 | 可写 | 只读 |
| --- | --- | --- |
| G（后端） | `internal/**` | `web/**` |
| W（前端） | `web/**` | `internal/**` |

合并顺序 G → W：前端要调的方法必须先存在。
