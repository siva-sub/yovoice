#!/usr/bin/env bash
# 一键实机基准：默认 dry-run，只有显式 --yes 才会申请 GPU。
#
#   bash scripts/colab/run-bench.sh --gpu T4
#   bash scripts/colab/run-bench.sh --gpu T4 --yes
#
# 流程：colab new → 上传 provision.sh / bench.py → 远端 provision + bench →
# 下载 bench-<GPU>.json → colab stop。失败或中断时也会尝试 stop，避免会话继续计费。
#
# 注意：`colab new` 本身没有 --keep 选项（--keep 是 `colab run` 的选项）；
# `new` 创建的会话本来就会保留到 `colab stop`，这里不用 `run` 是因为要把
# provision.sh 与 bench.py 两个文件上传到同一个运行时。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
ANALYZE="$REPO_ROOT/scripts/audiobook/analyze.py"

GPU=""
SESSION=""
REPEATS="2"
HOURS="1"
BALANCE=""
RECORDS="${HOME}/ss_audiobook/data/vol6.jsonl"
OUT=""
TIMEOUT=""
MODEL="breeze-tts-2"
YES=0

COLAB_BIN="${COLAB_BIN:-colab}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -t 1 ]]; then
	C_HEAD=$'\033[1m'
	C_OK=$'\033[32m'
	C_WARN=$'\033[33m'
	C_NO=$'\033[31m'
	C_OFF=$'\033[0m'
else
	C_HEAD=""
	C_OK=""
	C_WARN=""
	C_NO=""
	C_OFF=""
fi

usage() {
	cat <<'EOF'
用法：bash scripts/colab/run-bench.sh --gpu T4|A100 [选项]

默认只打印将执行的命令和预算，不申请 GPU、不消耗计算单元。
只有带 --yes 才会真正执行。

选项：
  --gpu T4|A100         必填；决定卡型与 bench 的标签
  --session NAME        会话名，默认 acpp-bench-<小写卡型>
  --repeats N           每条提示重复次数，默认 2
  --hours N             预算预留的 GPU 小时，默认 1（不是实测耗时）
  --balance UNITS       当前余额；不给则调用只读的 colab usage
  --records PATH        vol6.jsonl，用于 analyze.py cost 的整卷结论
  --model NAME          模型，默认 breeze-tts-2
  --out PATH            下载路径，默认 ./bench-<GPU>.json
  --timeout SECONDS     远端执行超时，默认 T4 7200 / A100 10800
  -h, --help            显示本帮助

退出码：0=成功/干跑，1=执行失败，2=参数错误。
EOF
}

need_value() {
	if [[ $# -lt 2 ]]; then
		printf '参数 %s 缺少值\n' "$1" >&2
		exit 2
	fi
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--gpu)
			need_value "$@"
			GPU=$2
			shift 2
			;;
		--session)
			need_value "$@"
			SESSION=$2
			shift 2
			;;
		--repeats)
			need_value "$@"
			REPEATS=$2
			shift 2
			;;
		--hours)
			need_value "$@"
			HOURS=$2
			shift 2
			;;
		--balance)
			need_value "$@"
			BALANCE=$2
			shift 2
			;;
		--records)
			need_value "$@"
			RECORDS=$2
			shift 2
			;;
		--model)
			need_value "$@"
			MODEL=$2
			shift 2
			;;
		--out)
			need_value "$@"
			OUT=$2
			shift 2
			;;
		--timeout)
			need_value "$@"
			TIMEOUT=$2
			shift 2
			;;
		--yes)
			YES=1
			shift
			;;
		-h | --help)
			usage
			exit 0
			;;
		*)
			printf '未知参数：%s\n' "$1" >&2
			usage >&2
			exit 2
			;;
	esac
done

case "$GPU" in
	T4 | A100) ;;
	"")
		printf '必须用 --gpu 指定 T4 或 A100\n' >&2
		exit 2
		;;
	*)
		printf '不支持的卡型：%s（只接受 T4 或 A100）\n' "$GPU" >&2
		exit 2
		;;
esac
if [[ ! "$REPEATS" =~ ^[1-9][0-9]*$ ]]; then
	printf '--repeats 必须是正整数：%s\n' "$REPEATS" >&2
	exit 2
fi
if [[ ! "$HOURS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
	printf '--hours 必须是正数：%s\n' "$HOURS" >&2
	exit 2
fi
if [[ -n "$BALANCE" && ! "$BALANCE" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
	printf '--balance 必须是正数：%s\n' "$BALANCE" >&2
	exit 2
fi
if [[ -n "$TIMEOUT" && ! "$TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
	printf '--timeout 必须是正整数：%s\n' "$TIMEOUT" >&2
	exit 2
fi

if [[ -z "$SESSION" ]]; then
	SESSION="acpp-bench-$(printf '%s' "$GPU" | tr '[:upper:]' '[:lower:]')"
fi
if [[ -z "$OUT" ]]; then
	OUT="./bench-${GPU}.json"
fi
if [[ -z "$TIMEOUT" ]]; then
	if [[ "$GPU" == "A100" ]]; then
		TIMEOUT="10800"
	else
		TIMEOUT="7200"
	fi
fi

with_timeout() {
	local seconds=$1
	shift
	if command -v timeout >/dev/null 2>&1; then
		timeout "$seconds" "$@"
	elif command -v gtimeout >/dev/null 2>&1; then
		gtimeout "$seconds" "$@"
	else
		"$@"
	fi
}

colab_path=""
if command -v "$COLAB_BIN" >/dev/null 2>&1; then
	colab_path=$(command -v "$COLAB_BIN")
fi
python_path=""
if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
	python_path=$(command -v "$PYTHON_BIN")
fi

# 余额未知时先读只读的 colab usage；它不租卡，也不消耗计算单元。
if [[ -z "$BALANCE" && -n "$colab_path" ]]; then
	if usage_output=$(with_timeout 20 "$COLAB_BIN" usage </dev/null 2>&1); then
		parsed_balance=$(printf '%s\n' "$usage_output" |
			awk '/^Current balance:/ {print $3; exit}')
		if [[ -n "$parsed_balance" ]]; then
			BALANCE=$parsed_balance
		fi
	fi
fi

price=""
units=""
conclusion=""
if [[ -n "$BALANCE" && -f "$RECORDS" && -f "$ANALYZE" && -n "$python_path" ]]; then
	cost_json=$(mktemp "${TMPDIR:-/tmp}/run-bench-cost.XXXXXX" 2>/dev/null) || cost_json=""
	if [[ -n "$cost_json" ]]; then
		if "$PYTHON_BIN" "$ANALYZE" cost \
			--records "$RECORDS" \
			--balance "$BALANCE" \
			--card "$GPU" \
			--model "$MODEL" \
			--json "$cost_json" >/dev/null 2>&1; then
			if IFS=$'\t' read -r units conclusion price \
				< <("$PYTHON_BIN" - "$cost_json" "$GPU" 2>/dev/null <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    summary = json.load(handle)
card = sys.argv[2]
print(
    f"{summary['selected']['units'][card]:.2f}\t"
    f"{summary['conclusion']}\t"
    f"{summary['card_units_per_hour'][card]:.2f}"
)
PY
			); then
				:
			fi
		fi
		rm -f "$cost_json"
	fi
fi

# 记录文件缺失或 cost 失败时，至少从 analyze.py 的单价表拿卡价，保证花费上限可算。
if [[ -z "$price" && -f "$ANALYZE" && -n "$python_path" ]]; then
	price=$("$PYTHON_BIN" - "$ANALYZE" "$GPU" 2>/dev/null <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("analyze", sys.argv[1])
if spec is None or spec.loader is None:
    raise SystemExit(1)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.CARD_UNITS_PER_HOUR[sys.argv[2]])
PY
	) || price=""
fi

reserve=""
if [[ -n "$price" ]]; then
	reserve=$(awk -v price="$price" -v hours="$HOURS" \
		'BEGIN { printf "%.2f", price * hours }')
fi

remote_runner() {
	cat <<PY
import subprocess
import sys

subprocess.run(["bash", "/content/provision.sh"], check=True)
subprocess.run([
    sys.executable, "/content/bench.py",
    "--gpu-label", "$GPU",
    "--repeats", "$REPEATS",
    "--skip-provision",
], check=True)
PY
}

print_budget() {
	printf '\n预估花费（预算上限，不是实测耗时）：\n'
	if [[ -n "$reserve" ]]; then
		printf '  %s 单价：%s 单元/GPU 小时 × 预留 %s 小时 = %s 单元\n' \
			"$GPU" "$price" "$HOURS" "$reserve"
		if [[ -n "$BALANCE" ]]; then
			remaining=$(awk -v balance="$BALANCE" -v reserve="$reserve" \
				'BEGIN { printf "%.2f", balance - reserve }')
			printf '  当前结余：%s → 按满 %s 小时计约剩 %s\n' \
				"$BALANCE" "$HOURS" "$remaining"
		else
			printf '  当前结余：未知（colab usage 失败或未给 --balance）\n'
		fi
	else
		printf '  无法从 analyze.py 读取 %s 单价；实际花费 = 租用墙钟 × 卡型单价。\n' "$GPU"
		if [[ -n "$BALANCE" ]]; then
			printf '  当前结余：%s\n' "$BALANCE"
		else
			printf '  当前结余：未知（colab usage 失败或未给 --balance）\n'
		fi
	fi
	if [[ -n "$units" && -n "$conclusion" ]]; then
		printf '  analyze.py cost 整卷结论：%s\n' "$conclusion"
		printf '  注意：本次只跑 bench，不渲染整卷；上面的整卷数字只用于对比余额。\n'
	fi
}

if [[ $YES -eq 0 ]]; then
	printf '%s== 干跑：不会申请 GPU，也不会消耗计算单元 ==%s\n' "$C_HEAD" "$C_OFF"
	printf '卡型：%s  会话：%s  重复：%s  超时：%s 秒\n' \
		"$GPU" "$SESSION" "$REPEATS" "$TIMEOUT"
	if [[ -z "$colab_path" ]]; then
		printf '%s提醒：本机找不到 %s，下面是纯命令预览。%s\n' "$C_WARN" "$COLAB_BIN" "$C_OFF"
	fi
	print_budget
	printf '\n将执行以下命令（只有加 --yes 才会执行）：\n'
	printf '  colab new --gpu %s --session %s\n' "$GPU" "$SESSION"
	printf '  colab upload -s %s %s /content/provision.sh\n' "$SESSION" "$SCRIPT_DIR/provision.sh"
	printf '  colab upload -s %s %s /content/bench.py\n' "$SESSION" "$SCRIPT_DIR/bench.py"
	printf '  colab exec -s %s --timeout %s -f <远程执行脚本>\n' "$SESSION" "$TIMEOUT"
	printf '  colab download -s %s /content/bench-%s.json %s\n' "$SESSION" "$GPU" "$OUT"
	printf '  colab stop -s %s\n' "$SESSION"
	printf '\n远程执行脚本内容：\n'
	remote_runner | sed 's/^/  /'
	printf '\n确认执行：bash %s --gpu %s --yes\n' "$0" "$GPU"
	exit 0
fi

printf '%s== 实机执行：将申请 %s GPU 并开始计费 ==%s\n' "$C_HEAD$C_NO" "$GPU" "$C_OFF"
if [[ -z "$colab_path" ]]; then
	printf '错误：找不到命令 %s；请先安装 Colab CLI 并登录。\n' "$COLAB_BIN" >&2
	exit 1
fi
if [[ -z "$BALANCE" ]]; then
	printf '错误：无法读取余额。先运行 colab usage，或用 --balance 指定后再加 --yes。\n' >&2
	exit 1
fi
if [[ -z "$python_path" ]]; then
	printf '错误：找不到 %s。\n' "$PYTHON_BIN" >&2
	exit 1
fi

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/run-bench.XXXXXX")
session_started=0
cleanup() {
	local code=$?
	trap - EXIT
	if [[ $session_started -eq 1 ]]; then
		printf '\n清理：colab stop -s %s\n' "$SESSION"
		"$COLAB_BIN" stop -s "$SESSION" >/dev/null 2>&1 || true
	fi
	if [[ -n "$tmpdir" ]]; then
		rm -rf "$tmpdir"
	fi
	exit "$code"
}
trap cleanup EXIT

run() {
	printf '+ %s\n' "$*"
	"$@"
}

remote_runner >"$tmpdir/remote.py"

run "$COLAB_BIN" new --gpu "$GPU" --session "$SESSION"
session_started=1
run "$COLAB_BIN" upload -s "$SESSION" "$SCRIPT_DIR/provision.sh" /content/provision.sh
run "$COLAB_BIN" upload -s "$SESSION" "$SCRIPT_DIR/bench.py" /content/bench.py
run "$COLAB_BIN" exec -s "$SESSION" --timeout "$TIMEOUT" -f "$tmpdir/remote.py"
mkdir -p "$(dirname "$OUT")"
run "$COLAB_BIN" download -s "$SESSION" "/content/bench-${GPU}.json" "$OUT"
run "$COLAB_BIN" stop -s "$SESSION"
session_started=0

printf '\n%s完成：%s%s\n' "$C_OK" "$OUT" "$C_OFF"
printf '核对 colab usage 的余额变化，并按 docs/colab-runbook.md 记录；不要把它当成整卷渲染已通过。\n'
