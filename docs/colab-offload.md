# 把推理卸载到 Colab

本页描述两种把模型放到远端 GPU、而工作流留在本机的做法，以及一台机器上测多档 GPU 的流程。
它对应的是这样一条需求：本机只有 4 GB 显存，而 yovoice 的 Linux 引擎只发行 CPU 与 Vulkan
两种后端，官方 Ubuntu 预编译包同样没有 CUDA（见 audio.cpp 的 Prebuilt Binaries 表）。因此
**CUDA 只能自编译**，而自编译最省事的地方是有卡可借的 Colab。

## 引擎契约（先看这一节，其余都是它的推论）

`internal/engine/engine.go` 的 `start` 把推理进程当作子进程管理，契约只有四条：

1. 父进程先 `net.Listen("tcp4", "127.0.0.1:0")` 取一个空闲端口，随即关闭，把端口号写进配置。
2. 配置写到 `<数据目录>/runtime/server.json`，字段固定：

   ```json
   {"host":"127.0.0.1","port":PORT,"backend":"cuda|cpu|vulkan|metal","device":0,
    "threads":N,"lazy_load":true,"max_loaded_models":1,"idle_unload_ms":300000,
    "log_request_body":false,"max_request_body_bytes":1048576,
    "models":[{"id":"index","family":"breeze_tts","path":"/绝对路径/model.gguf",
               "task":"tts","mode":"offline","load_options":{},"session_options":{}}]}
   ```

3. 以 `exec.Command(可执行文件, "--config", 配置路径, "--no-ui")` 启动，工作目录是引擎所在目录；
   可执行文件路径来自环境变量 `WORKBENCH_ENGINE`（桌面宿主在 `desktop/linux/service.go:150` 注入）。
4. 就绪判定是 `GET http://127.0.0.1:PORT/health` 返回 200，最多 120 次、每次间隔 250 ms；
   之后所有生成请求都打到 `http://127.0.0.1:PORT/`。

注意第 1 条与第 3 条之间的空隙：**父进程只认「我启动了一个子进程，它在我给的端口上提供了
/health」**。不检查子进程是谁、也不检查它是否真的加载了模型。这就是两个卸载方案都能成立的原因。

## 方案 A：只把引擎搬走（交互式界面照常用）

本机跑 `yovoice-service` + 界面 + 工程状态 + 混音，**只有推理**走远端。适合逐行改音色、听
take、调锚点这类交互工作。

```
界面 → 本机 workbench → 本机 internal/engine → engine-shim（本机子进程）
                                                │  改写 models[].path 为远端路径
                                                │  ssh 远端启动 audiocpp_server --config
                                                └─ 127.0.0.1:PORT ──隧道──▶ 远端:PORT
```

`scripts/colab/engine-shim.py` 实现的就是这个子进程。它满足契约的四条：

- 接受 `--config <路径> --no-ui`，读 `server.json` 里的 `port` 与 `models`；
- 把 `models[].path` 换成远端路径（`--remote-model-root` 映射），其余字段原样保留；
- 通过 `colab ssh --proxy-mode` 起的隧道，在远端执行 `audiocpp_server --config`；
- 在本机 `127.0.0.1:PORT` 上监听并把 HTTP 原样转发，于是 `/health` 与生成请求都成立。

启用方式（桌面宿主读同一条环境变量）：

```bash
export WORKBENCH_ENGINE=/home/siva/yovoice/scripts/colab/engine-shim.py
# 可选：远端模型根目录映射、会话名、CUDA 构建路径
export COLAB_ENGINE_SESSION=acpp
export COLAB_ENGINE_REMOTE_ROOT=/content/models
export COLAB_ENGINE_REMOTE_BIN=/content/audio.cpp/build/bin/audiocpp_server
```

本机**仍然需要**模型文件存在，因为 `engine.start` 会先 `os.Stat(model)`（缺失报
`ErrModelMoved`）。做法是让本机的模型目录指向一份占位文件或用符号链接指向同名空文件；
shim 转发时用的是远端真实路径。

## 方案 B：整栈搬走（批量渲染）

`yovoice serve` 在 Colab 上跑，本机用 HTTP / MCP 驱动。界面不参与，适合 8000+ 行的整章队列。

```bash
# 远端
export YOVOICE_API_TOKEN=$(openssl rand -hex 32)
yovoice serve --data-dir ./server-data --listen 127.0.0.1:8080 --generation-timeout 60m
# 本机
ssh -N -L 8080:127.0.0.1:8080 <colab>   # 或走 colab ssh --proxy-mode
curl -H "Authorization: Bearer $T" http://127.0.0.1:8080/v1/status
```

`yovoice serve` 只提供 API 与 MCP，不提供界面（`cmd/yovoice/serve.go` 里没有 web 目录相关逻辑），
所以这个方案天然是批处理式的。

## 多档 GPU 实测

`scripts/colab/bench.py` 在 Colab 内部执行，被 `colab run --gpu X` 租一次卡就跑完：

```bash
python3 scripts/colab/bench.py --gpu-label T4
# 或者交给 CLI 自己租卡；脚本本身不需要知道卡型
colab run --gpu T4    scripts/colab/bench.py --gpu-label T4
colab run --gpu L4    scripts/colab/bench.py --gpu-label L4
colab run --gpu A100  scripts/colab/bench.py --gpu-label A100 --high-mem
colab run --gpu G4    scripts/colab/bench.py --gpu-label G4
```

脚本做三件事：编译带 CUDA 的 audio.cpp（多架构 `75;80;89;90`，一次编译覆盖 T4/L4/A100/H100）、
下载 `audio-cpp/audio.cpp-gguf` 里未设门禁的 GGUF、用固定提示与固定种子跑一组 SFX 与 TTS，
最后把 `GPU 名 / 显存 / 墙钟 / RTF / 峰值显存` 写进 JSON。

要点：

- **编译时间主导整轮耗时**，所以 `--cache-dir` 可以指向 Google Drive 挂载点，把 `build/` 复用；
  注意 `colab drivemount` 需要人工确认，代理环境跑不了。
- 峰值显存用 `nvidia-smi --query-gpu=memory.used --format=csv` 采样，不要只看模型文件大小。
- 判定「更快」只看同一提示同一长度下的 RTF，不要拿不同 `duration_seconds` 的结果互比。

## 责任边界

- 方案 A 不改 yovoice 任何 Go 代码，风险集中在一个 Python 子进程上；出问题就 `unset WORKBENCH_ENGINE` 回到本地引擎。
- 两个方案都要求远端与本地 `audio.cpp` 版本一致（配置字段、路由都可能变），把版本号写进结果 JSON。
- Colab 单次会话会被回收，方案 A 的隧道断了界面上的生成会报错；shim 退出即释放，重试会重新租用。
