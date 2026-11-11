#!/usr/bin/env bash
# 在 Colab（Linux x64 + CUDA）上准备 audio.cpp 引擎与模型。
# 幂等：已经编译过、已经下载过的文件都会跳过，可以反复执行。
#
#   bash scripts/colab/provision.sh                 # 默认 /content 下准备
#   AUDIOCPP_ROOT=/content/audio.cpp bash ...
#   CUDA_ARCHS="75;80;89" bash ...                  # 覆盖编译架构
#
# 官方 Ubuntu 预编译包只有 CPU 与 Vulkan，没有 CUDA，所以这里必须自编译。
set -euo pipefail

AUDIOCPP_ROOT=${AUDIOCPP_ROOT:-/content/audio.cpp}
MODELS_ROOT=${MODELS_ROOT:-/content/models}
REPO=${AUDIOCPP_REPO:-https://github.com/0xShug0/audio.cpp}
# 一次编译覆盖 T4(75)/A100(80)/L4(89)/H100(90)，换卡时不必重编。
CUDA_ARCHS=${CUDA_ARCHS:-"75;80;89;90"}
JOBS=${JOBS:-$(nproc)}

log() { printf '\033[1maudiocpp\033[0m %s\n' "$*"; }

# nvcc 是硬前提：没有它就只能拿到 CPU 后端，本脚本不做降级。
if ! command -v nvcc >/dev/null 2>&1; then
	echo "没有找到 nvcc。请先确认这是 GPU 运行时（colab new --gpu ...），并安装 CUDA Toolkit。" >&2
	exit 1
fi
nvcc --version | tail -2

if [ ! -d "$AUDIOCPP_ROOT/.git" ]; then
	log "克隆 audio.cpp 到 $AUDIOCPP_ROOT"
	mkdir -p "$(dirname "$AUDIOCPP_ROOT")"
	git clone --depth 1 --recurse-submodules "$REPO" "$AUDIOCPP_ROOT"
fi
cd "$AUDIOCPP_ROOT"
log "audio.cpp 版本：$(git rev-parse --short HEAD)"

# 构建产物落在 build/ 下；已存在就直接复用，避免每次租卡都重编十分钟。
if [ -x build/bin/audiocpp_server ] && [ -x build/bin/audiocpp_cli ]; then
	log "已有 CUDA 构建，跳过编译"
else
	log "编译 audiocpp_server / audiocpp_cli（架构 $CUDA_ARCHS，并行 $JOBS）"
	cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHS" -DCMAKE_BUILD_TYPE=Release
	cmake --build build -j"$JOBS" --target audiocpp_server audiocpp_cli
fi
[ -x build/bin/audiocpp_server ] || {
	echo "没有生成 audiocpp_server，编译失败。" >&2
	exit 1
}

log "下载 GGUF 到 $MODELS_ROOT（audio-cpp/audio.cpp-gguf 未设门禁）"
python3 -m pip install -q -U huggingface_hub
mkdir -p "$MODELS_ROOT"
python3 - "$MODELS_ROOT" <<'PY'
import sys
from pathlib import Path
from huggingface_hub import snapshot_download

root = Path(sys.argv[1])
# 只取工作流用得到的两个家族：音效与带音色指令的 TTS。
wanted = [
    "Stable-Audio-3-Small-SFX-GGUF/stable-audio-3-small-sfx-q8_0.gguf",
    "Breeze-TTS-2-GGUF/breeze-tts-2-q8_0.gguf",
]
missing = [w for w in wanted if not (root / w).exists()]
if not missing:
    print("模型已齐全")
else:
    snapshot_download("audio-cpp/audio.cpp-gguf", local_dir=str(root), allow_patterns=missing)
    print("下载完成：" + ", ".join(missing))
PY

log "完成"
log "  引擎  $AUDIOCPP_ROOT/build/bin/audiocpp_server"
log "  模型  $MODELS_ROOT"
log "下一步：python3 scripts/colab/bench.py --gpu-label \${GPU_LABEL:-unknown}"
