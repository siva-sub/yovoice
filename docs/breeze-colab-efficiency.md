# Breeze TTS 2 在 Colab 上怎么跑才快

结论先行：**没有现成的 Colab notebook**，但有两条被我们漏掉的加速路径，其中一条能把
8,794 次单条请求压成约 69 次批量请求。选错卡会让全部加速失效——这才是 0.48× 与 3.1× 的来源。

## 一、GH 上到底有没有 Colab 脚本

用 `gh search repos` / `gh api .../git/trees` 逐个查过，**四仓库零 notebook**：

| 仓库 | 星 | colab/ipynb | 说明 |
| --- | --- | --- | --- |
| `breezeblue-ai/breeze-tts` | 1645 | 无 | 官方 PyTorch 推理，含 FastAPI 服务与批量端点 |
| `HoppouAI/Breeze-TTS-2.cpp` | 171 | 无 | C++/GGUF 运行时，脚本只有 gguf 转换与打包 |
| `RACErace/breeze-tts-8g` | 4 | 无 | 官方代码的 8 GB 加速配方（FlashAttention-2） |
| `Sentinel3252/...-vLLM-Omni` | 86 | 无 | vLLM 集成，**明确未支持克隆/CFG，无性能数据** |

所以 `scripts/colab/*` 仍是唯一路径，但下面两件事必须改。

## 二、加速路径 1：官方 HTTP 服务 + 批量端点

`breeze_infer/api.py` 是一个 FastAPI 服务，不是只能单条推理的 CLI：

- `GET /health`、`GET /v1/model`
- `POST /v1/audio/speech` —— 单条，但 `texts` 可传**列表**
- `POST /v1/audio/speech/batch` —— `MAX_BATCH_TEXTS = 128`，一次合成整批

关键实现细节（来自源码，不是文档措辞）：

1. 服务启动时构建 `BatchDepthDecoder(model, max_batch_size=128)`，即**深度解码是整批一起做**
   的 CUDA Graph 路径。我们逐条请求等于把这条路径关掉。
2. 批量响应是**拼接的 s16le PCM**，按 `X-Segment-Bytes` 头切段——因此切分逻辑必须严格，
   切错一个字节后面全错位。
3. 批量时**文本编码走 eager**（`eager_batch_modules` 会关掉 fast text-encoder graph），
   这是批量的代价，不是 bug。
4. `_request_lock` 非阻塞：同一时刻只允许一个推理请求，**并发请求会拿到 409**。
   所以并行度靠**批量宽度**，不是靠并发连接数。

对我们的意义：8,794 条 cue ÷ 128 ≈ **69 次请求**，而不是 8,794 次进程调用。

## 三、加速路径 2：算力代次决定一切（这就是 0.48× 的真相）

官方 README：**RTF 0.32（约 3.1× 实时）是 H100 + 预热 fast path**；我们的成本表用的是
第三方基准的 **0.48× 实时（≈ RTF 2.08）**。两者差 6.5 倍，不矛盾——因为加速栈按代次门票：

| Colab 卡 | 算力 | 显存 | FlashAttention-2 | CUDA Graph fast path | 结论 |
| --- | --- | --- | --- | --- | --- |
| **T4** | **sm75** | 16 GB | ❌ 需 sm80+ | 受限 | **最不该用**：加速栈全部失效，接近基准的 0.48× |
| **L4** | sm89 | 24 GB | ✅ | ✅（`--fast-all` 需 14.4 GiB，装得下） | **性价比首选**，先测它 |
| **G4** | 需实测 | 需实测 | 视代次 | 视代次 | 先 `nvidia-smi --query-gpu=compute_cap` 再决定 |
| **A100** | sm80 | 40/80 GB | ✅（编译要 `FLASH_ATTN_CUDA_ARCHS=80`） | ✅ | 吞吐最高，费用也最高 |
| H100 | sm90 | 80 GB | ✅ | ✅ | 本账号无权限 |

`--fast-all` 要 14.4 GiB：**T4 的 16 GB 太紧**，L4/A100 才有余量；eager 约 7.7 GiB。

## 四、因此的推荐顺序

1. **L4 先跑**：sm89 支持 flash-attn，24 GB 装得下 `--fast-all`，单位成本远低于 A100。
   用 `bench.py` 依次测 eager / `--fast-all` / 批量三种，把 RTF 记下来。
2. **再 A100**：同样的矩阵，编译时带 `FLASH_ATTN_CUDA_ARCHS=80`。如果 L4 的批量 RTF
   已经够用，A100 只是省时间不是省钱的答案。
3. **G4 先辨认代次**再用，不要盲信“更新一代就更快”。
4. **T4 只用于音效/配乐车道**（Stable Audio 3 那类），不要用来渲 Breeze。

预算口径也要跟着改：按官方 3.1× 实时，22 小时音频约 **7 GPU 小时**；按基准 0.48× 是
**45.7 小时**。在实测之前，这两者之间差一个数量级的钱，所以**先测一天再排整卷**。

## 五、一个值得做的附带实验

官方 Breeze 服务也有 `GET /health`，与 yovoice 的引擎契约（spawn → 轮询 `/health` → 全量请求
转发）形状相同。如果能用一个 shim 把 yovoice 的 `WORKBENCH_ENGINE` 指向这个服务，
就不必走 `audio.cpp` 的 CUDA 自编译路线。**但响应体格式（`/v1/audio/speech` vs
`audiocpp_server`）不同，这是假设不是结论**，需要一次真实对照才能定。

## 六、还没验证的事

- 上述 RTF 全部来自官方 README（H100）与第三方基准（未标注卡型），**本机与 Colab 都未实测**。
- `G4` 的算力代次未确认。
- 批量端点在长文本（>600 字）上的切块行为未验证。
- 8 GB 配方的 6.3 GiB 预留是社区分叉的声明，未独立复核。

## 七、选卡器（`scripts/colab/card-fit.py`）

把上面的判断做成可执行的一步，避免靠记忆选卡：

```
$ python3 scripts/colab/card-fit.py --card T4 --balance 265.33 --unit-rate 11.8
算力 sm7.5 · 显存 16 GB → eager-only
  FlashAttention-2: 不可用
  --fast-all:       不可用
  GPU 小时  乐观 7.0（RTF 0.32） / 悲观 45.7（RTF 2.08）
  计算单元  乐观 82.9 / 悲观 539.6
```

它的价值不是那两行数字，而是**明确标出乐观值只在「有 FlashAttention-2 且能开
`--fast-all`」时才允许按 RTF 0.32 估**。T4 上只会给你悲观值——这正是之前成本表
算出 538 单元的原因。

`--unit-rate` 必须自己给：计算单元价格随卡型与本账号条款变化，脚本不猜。
之前成本表的 11.8/小时只是口径示例，实际以 `colab usage` 为准。
