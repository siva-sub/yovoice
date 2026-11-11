# ADR-0005：推理卸载到 Colab，界面与混音留在本机

- 状态：已实现脚手架（`scripts/colab/`），待实机验证
- 日期：2026-11-11
- 相关：ADR-0003
- 实施细节见 [docs/colab-offload.md](../colab-offload.md)

## 背景

本机是 4 GB 显存的笔记本 GPU，而 yovoice 在 Linux 上只发行 CPU 与 Vulkan 引擎，audio.cpp
的 Ubuntu 预编译包同样没有 CUDA。要在本地跑 3.47B 的 Breeze 或 35B 的 Drama 3 不现实。

同时，整条流水线里真正需要 GPU 的只有推理：归因、cue 编排、侧链混音、封装 m4b 全是 CPU 活，
本地跑完全够用，而且本卷的既定做法就是"片段在 Colab 生成，混音只在本机做"。

## 决策

`internal/engine` 把推理当子进程管理，只检查四件事：从 `server.json` 读端口、
用 `--config … --no-ui` 启动 `WORKBENCH_ENGINE` 指向的可执行文件、
`GET /health` 返回 200、之后所有请求打 `http://127.0.0.1:PORT/`。
**它不验证子进程是谁，也不验证模型是否真的加载了。**

利用这个缝隙做两个方案：

**A（默认，交互式）** `scripts/colab/engine-shim.py` 作为 `WORKBENCH_ENGINE` 启动：
改写 `models[].path` 到远端路径，经 `colab ssh --proxy-mode` 在 Colab 上起 `audiocpp_server`，
把本机 `127.0.0.1:PORT` 转发过去。界面、工程状态、混音全在本机，只有推理出网。不改一行 Go。

**B（批量）** `yovoice serve` 跑在 Colab，本机用 HTTP / MCP 驱动。`serve` 不提供界面，
所以这条只用于整卷队列。

GPU 按车道分配，理由与 ADR-0003 一致：

| 车道 | 卡 | 理由 |
| --- | --- | --- |
| 音效（273 cue ≈ 9 分钟音频） | T4 / L4 | 568M、峰值 3.65 GB，任何卡都是几分钟的事 |
| 人声（22 小时音频） | A100 | 唯一有真实吞吐量的车道，模型要常驻 |
| 配乐（按篇章） | L4 | ACE-Step Q8 6.19 GB，一次跑完 |

三条硬规则：

1. **每条车道一个 `colab new --keep`，用 `exec`/`serve` 排队跑，不要逐章 `colab run`**——
   每次 `colab run` 都会重新租卡并重新加载模型。
2. **冷启动是最大的可避免损失。** 基准里 Qwen3-TTS 1.7B 冷启 0.25×、热启 3.58×，差 **14 倍**。
3. **一次编译多架构**（`75;80;89;90`）覆盖 T4/A100/L4/H100，换卡不重编。

## 依据

- `internal/engine/engine.go` 的 `start`：端口、`server.json` 字段、`--config/--no-ui`、`/health` 探测
- `desktop/linux/service.go:150` 注入 `WORKBENCH_ENGINE`
- audio.cpp 的 Prebuilt Binaries 表：Ubuntu x64 只有 CPU 与 Vulkan
- 实测：Qwen3-TTS 1.7B 冷 0.25× / 热 3.58×；Stable Audio 3 Small SFX 峰值 3.65 GB

## 后果

- 本机仍需存在同名模型文件（`engine.start` 会先 `os.Stat`），用符号链接占位即可
- 远端与本地 audio.cpp 版本必须一致，结果里要记 commit
- 隧道断开时界面上的生成会失败，重试即重新租用；**Colab 会话会被回收，不能当作持久服务**
- `colab` 账号的 GPU 资格以实际接口为准（当前可用 G4/A100/L4/T4，H100 不可用）

## 备选方案

- **改 Go 加远端引擎模式**：更干净，但要动 `internal/engine` 的启动与生命周期，收益相同
- **整栈跑在 Colab 用浏览器界面**：服务端口随机、带会话 cookie 与 Origin 校验，隧道下很脆弱
