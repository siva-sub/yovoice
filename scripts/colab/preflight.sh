#!/usr/bin/env bash
# Colab 本地预检：只读检查，不申请 GPU、不消耗计算单元。
#
#   bash scripts/colab/preflight.sh
#   bash scripts/colab/preflight.sh --records ... --balance 265.33 --card T4
#
# 唯一的远程调用是只读的 `colab usage`（读余额，不租卡）。退出码：
#   0 = GO，1 = NO-GO，2 = 参数错误。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
ANALYZE="$REPO_ROOT/scripts/audiobook/analyze.py"

RECORDS="${HOME}/ss_audiobook/data/vol6.jsonl"
BALANCE=""
CARD="T4"
MODEL="breeze-tts-2"
MODELS_DIR="${MODELS_ROOT:-${YOVOICE_MODELS_DIR:-${HOME}/ss_audiobook/models}}"
MIN_FREE_GB="10"
RESERVE_HOURS="1"

# 下面的命令名可以被测试或特殊安装覆盖；默认就是 PATH 里的名字。
COLAB_BIN="${COLAB_BIN:-colab}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
COLAB_CONFIG_DIR="${COLAB_CONFIG_DIR:-${HOME}/.config/colab-cli}"

if [[ -t 1 ]]; then
	C_OK=$'\033[32m'
	C_NO=$'\033[31m'
	C_WARN=$'\033[33m'
	C_HEAD=$'\033[1m'
	C_OFF=$'\033[0m'
else
	C_OK=""
	C_NO=""
	C_WARN=""
	C_HEAD=""
	C_OFF=""
fi

pass_count=0
fail_count=0
warn_count=0
missing_items=""
warn_items=""

ok() {
	pass_count=$((pass_count + 1))
	printf '%s[OK]%s %-10s %s\n' "$C_OK" "$C_OFF" "$1" "$2"
}

no() {
	fail_count=$((fail_count + 1))
	missing_items="${missing_items}${missing_items:+、}$1"
	printf '%s[NO]%s %-10s %s\n' "$C_NO" "$C_OFF" "$1" "$2"
}

warn() {
	warn_count=$((warn_count + 1))
	warn_items="${warn_items}${warn_items:+、}$1"
	printf '%s[!]%s  %-10s %s\n' "$C_WARN" "$C_OFF" "$1" "$2"
}

usage() {
	cat <<'EOF'
用法：bash scripts/colab/preflight.sh [选项]

不申请 GPU、不消耗计算单元；只检查本机是否具备起一次 Colab 基准的条件。

选项：
  --records PATH        vol6.jsonl 路径（默认 ~/ss_audiobook/data/vol6.jsonl）
  --balance UNITS       当前计算单元余额；不给则调用只读的 colab usage 读取
  --card NAME           卡型，默认 T4（可传 analyze.py 支持的任何卡型）
  --model NAME          模型，默认 breeze-tts-2
  --models-dir PATH     GGUF 缓存目录（默认 ~/ss_audiobook/models；也可用 MODELS_ROOT）
  --min-free-gb N       磁盘余量下限，默认 10
  --hours N             单次基准预留 GPU 小时，默认 1
  -h, --help            显示本帮助

退出码：0=GO，1=NO-GO，2=参数错误。
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
		--records)
			need_value "$@"
			RECORDS=$2
			shift 2
			;;
		--balance)
			need_value "$@"
			BALANCE=$2
			shift 2
			;;
		--card)
			need_value "$@"
			CARD=$2
			shift 2
			;;
		--model)
			need_value "$@"
			MODEL=$2
			shift 2
			;;
		--models-dir | --models-root)
			need_value "$@"
			MODELS_DIR=$2
			shift 2
			;;
		--min-free-gb)
			need_value "$@"
			MIN_FREE_GB=$2
			shift 2
			;;
		--hours)
			need_value "$@"
			RESERVE_HOURS=$2
			shift 2
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

# 数值参数先挡住，避免把 -1 或空串传给 analyze.py 后得到难读的报错。
if [[ -n "$BALANCE" && ! "$BALANCE" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
	printf '余额必须是正数：%s\n' "$BALANCE" >&2
	exit 2
fi
if [[ ! "$MIN_FREE_GB" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
	printf '磁盘下限必须是正数：%s\n' "$MIN_FREE_GB" >&2
	exit 2
fi
if [[ ! "$RESERVE_HOURS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
	printf '预留小时数必须是正数：%s\n' "$RESERVE_HOURS" >&2
	exit 2
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

printf '%s== Colab 预检：只读检查，不申请 GPU ==%s\n' "$C_HEAD" "$C_OFF"
printf '记录：%s\n卡型：%s  模型：%s\n\n' "$RECORDS" "$CARD" "$MODEL"

# ---------- 基础命令 ----------

if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
	python_path=$(command -v "$PYTHON_BIN")
	ok "python" "$python_path"
else
	python_path=""
	no "python" "找不到命令 $PYTHON_BIN（analyze.py 需要 Python 3）"
fi

colab_path=""
if command -v "$COLAB_BIN" >/dev/null 2>&1; then
	colab_path=$(command -v "$COLAB_BIN")
	version_line=$(with_timeout 10 "$COLAB_BIN" version </dev/null 2>&1 |
		grep -E '^Version:' | tail -n 1 || true)
	ok "colab" "$colab_path${version_line:+ · $version_line}"
else
	no "colab" "找不到命令 $COLAB_BIN（先安装 Colab CLI 并登录）"
fi

if command -v "$FFMPEG_BIN" >/dev/null 2>&1; then
	ok "ffmpeg" "$(command -v "$FFMPEG_BIN")"
else
	no "ffmpeg" "找不到命令 $FFMPEG_BIN（m4b/m4a 解码需要）"
fi

# ---------- 登录态与余额 ----------

token_path="$COLAB_CONFIG_DIR/token.json"
adc_path="${GOOGLE_APPLICATION_CREDENTIALS:-${HOME}/.config/gcloud/application_default_credentials.json}"
login_local=""
if [[ -n "$python_path" && -s "$token_path" ]] &&
	"$PYTHON_BIN" - "$token_path" >/dev/null 2>&1 <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
raise SystemExit(0 if data.get("refresh_token") else 1)
PY
then
	login_local="token.json"
elif [[ -s "$adc_path" ]]; then
	login_local="ADC"
fi

usage_ok=0
usage_detail=""
if [[ -z "$BALANCE" && -n "$colab_path" ]]; then
	if usage_output=$(with_timeout 20 "$COLAB_BIN" usage </dev/null 2>&1); then
		parsed_balance=$(printf '%s\n' "$usage_output" |
			awk '/^Current balance:/ {print $3; exit}')
		if [[ -n "$parsed_balance" ]]; then
			BALANCE=$parsed_balance
			usage_ok=1
		else
			usage_detail="输出里没有 Current balance 行"
		fi
	else
		usage_detail=$(printf '%s\n' "$usage_output" | tail -n 1)
	fi
fi

if [[ -n "$BALANCE" && ( -n "$login_local" || $usage_ok -eq 1 ) ]]; then
	ok "登录态" "本地凭据：${login_local:-colab usage} · 余额 $BALANCE"
elif [[ -z "$BALANCE" ]]; then
	no "登录态" "找不到可用凭据，且 colab usage 未给出余额${usage_detail:+（$usage_detail）}"
else
	no "登录态" "找到了余额但没有本地凭据；先运行 colab usage 完成登录"
fi

if [[ -z "$BALANCE" ]]; then
	no "余额" "未知；用 --balance 指定，或先让 colab usage 成功"
fi

# ---------- GGUF 缓存与磁盘 ----------

if [[ -d "$MODELS_DIR" ]]; then
	missing_models=""
	for relative in \
		"Breeze-TTS-2-GGUF/breeze-tts-2-q8_0.gguf" \
		"Stable-Audio-3-Small-SFX-GGUF/stable-audio-3-small-sfx-q8_0.gguf"; do
		if [[ ! -s "$MODELS_DIR/$relative" ]]; then
			missing_models="${missing_models}${missing_models:+、}$relative"
		fi
	done
	if [[ -n "$missing_models" ]]; then
		no "GGUF 缓存" "$MODELS_DIR 缺少 $missing_models"
	else
		ok "GGUF 缓存" "$MODELS_DIR（两个基准 GGUF 已就绪）"
	fi
else
	no "GGUF 缓存" "缺目录 $MODELS_DIR（可用 --models-dir 指定；provision.sh 会在 Colab 重新下载）"
fi

disk_probe=$MODELS_DIR
while [[ ! -e "$disk_probe" ]]; do
	parent=$(dirname "$disk_probe")
	[[ "$parent" == "$disk_probe" ]] && break
	disk_probe=$parent
done
if free_kb=$(df -Pk "$disk_probe" 2>/dev/null | awk 'NR==2 {print $4}') && [[ -n "$free_kb" ]]; then
	free_gb=$(awk -v kb="$free_kb" 'BEGIN { printf "%.1f", kb / 1024 / 1024 }')
	if awk -v free="$free_gb" -v minimum="$MIN_FREE_GB" \
		'BEGIN { exit !(free + 0 >= minimum + 0) }'; then
		ok "磁盘" "$disk_probe 剩余 ${free_gb} GB ≥ ${MIN_FREE_GB} GB"
	else
		no "磁盘" "$disk_probe 剩余 ${free_gb} GB < ${MIN_FREE_GB} GB"
	fi
else
	no "磁盘" "无法读取 $disk_probe 的剩余空间"
fi

# ---------- analyze.py cost ----------

if [[ ! -f "$ANALYZE" ]]; then
	no "analyze.py" "找不到 $ANALYZE"
elif [[ -n "$BALANCE" ]]; then
	if [[ ! -f "$RECORDS" ]]; then
		no "预算" "找不到记录文件 $RECORDS"
	else
		tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/colab-preflight.XXXXXX")
		trap 'rm -rf "$tmpdir"' EXIT
		cost_json="$tmpdir/cost.json"
		if "$PYTHON_BIN" "$ANALYZE" cost \
			--records "$RECORDS" \
			--balance "$BALANCE" \
			--card "$CARD" \
			--model "$MODEL" \
			--json "$cost_json" >"$tmpdir/cost.out" 2>"$tmpdir/cost.err"; then
			if IFS=$'\t' read -r selected_units selected_balance card_price conclusion \
				< <("$PYTHON_BIN" - "$cost_json" "$CARD" 2>/dev/null <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    summary = json.load(handle)
card = sys.argv[2]
print(
    f"{summary['selected']['units'][card]:.2f}\t"
    f"{summary['balance']:.2f}\t"
    f"{summary['card_units_per_hour'][card]:.2f}\t"
    f"{summary['conclusion']}"
)
PY
			); then
				if [[ -z "${selected_units:-}" || -z "${card_price:-}" ]]; then
					no "预算" "analyze.py cost 的 JSON 缺少卡型 $CARD 的单价"
				else
					if awk -v units="$selected_units" -v balance="$selected_balance" \
						'BEGIN { exit !(units + 0 <= balance + 0.0001) }'; then
						ok "预算整卷" "$conclusion"
					else
						warn "预算整卷" "$conclusion（整卷不够，单次基准仍可能够）"
					fi
					reserve=$(awk -v price="$card_price" -v hours="$RESERVE_HOURS" \
						'BEGIN { printf "%.2f", price * hours }')
					if awk -v reserve="$reserve" -v balance="$selected_balance" \
						'BEGIN { exit !(reserve + 0 <= balance + 0.0001) }'; then
						ok "预算单次" "预留 ${RESERVE_HOURS} GPU 小时 × ${card_price} = ${reserve} 单元；结余 ${selected_balance}"
					else
						no "预算单次" "预留 ${RESERVE_HOURS} GPU 小时 × ${card_price} = ${reserve} 单元 > 结余 ${selected_balance}"
					fi
				fi
			else
				no "预算" "无法解析 analyze.py cost 的 JSON"
			fi
		else
			cost_error=$(tail -n 1 "$tmpdir/cost.err" 2>/dev/null || true)
			no "预算" "analyze.py cost 失败${cost_error:+：$cost_error}"
		fi
	fi
fi

# ---------- 汇总 ----------

printf '\n'
if [[ $fail_count -eq 0 ]]; then
	printf '%s结论：GO%s\n' "$C_HEAD$C_OK" "$C_OFF"
else
	printf '%s结论：NO-GO（%d 项）%s\n' "$C_HEAD$C_NO" "$fail_count" "$C_OFF"
	printf '缺什么：%s\n' "$missing_items"
fi
if [[ $warn_count -gt 0 ]]; then
	printf '提醒：%s\n' "$warn_items"
fi
printf '\n'

# 最后一行必须固定，提醒这是只读预检，没有产生 GPU 费用。
if [[ -t 1 ]]; then
	printf '\033[1;33m%s\033[0m\n' '未执行任何 GPU 作业；执行前看 docs/colab-runbook.md'
else
	printf '%s\n' '未执行任何 GPU 作业；执行前看 docs/colab-runbook.md'
fi

if [[ $fail_count -gt 0 ]]; then
	exit 1
fi
exit 0
