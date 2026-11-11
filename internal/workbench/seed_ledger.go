package workbench

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"yovoice/internal/platform"
)

// SeedKey 是种子账本的键（ADR-0009）：同一组模型、锚点、文本、演绎指令、CFG 与种子
// 必须重放出同一条音频；任何一项变化都应当得到新的键。
type SeedKey struct {
	ModelID     string  `json:"modelId"`
	AnchorRev   string  `json:"anchorRev,omitempty"`
	TextHash    string  `json:"textHash"`
	Instruction string  `json:"instruction,omitempty"`
	CFG         float64 `json:"cfg"`
	Seed        int     `json:"seed"`
}

// SeedLedgerEntry 是一条已记录的生成结果：键相同则指纹必须相同，否则说明模型或引擎变了。
type SeedLedgerEntry struct {
	Key         SeedKey   `json:"key"`
	Fingerprint string    `json:"fingerprint"`
	CreatedAt   time.Time `json:"createdAt"`
}

// SeedStatus 是写入或重放种子账本的结果。
type SeedStatus string

const (
	SeedRecorded           SeedStatus = "recorded"            // 首次记录
	SeedReplayed           SeedStatus = "replayed"            // 同键同指纹，可放心复用
	SeedVersionMismatch    SeedStatus = "version-mismatch"    // 同键不同指纹，必须在 take 上标版本不符
	SeedFingerprintMissing SeedStatus = "fingerprint-missing" // 没有指纹，无法入账
)

// SeedLedger 是种子账本；Entries 按记录顺序保留，同一键始终以第一次的指纹为准。
type SeedLedger struct {
	Entries []SeedLedgerEntry `json:"entries"`
}

// SeedKeyForCue 把一条渲染 cue 转成账本键；种子为空（自动随机）时无法重放，返回 false。
func SeedKeyForCue(cue RenderCue) (SeedKey, bool) {
	if cue.Seed == nil {
		return SeedKey{}, false
	}
	return SeedKey{
		ModelID:     cue.ModelID,
		AnchorRev:   cue.AnchorRev,
		TextHash:    TextHash(cue.Text),
		Instruction: cue.Instruction,
		CFG:         cue.CFG,
		Seed:        *cue.Seed,
	}, true
}

// Record 把一次生成结果记入账本。同键同指纹返回 SeedReplayed；同键不同指纹返回
// SeedVersionMismatch，并保留第一次的指纹，便于解释是哪一版开始对不上。
func (l *SeedLedger) Record(key SeedKey, fingerprint string) SeedStatus {
	fingerprint = strings.TrimSpace(fingerprint)
	if l == nil || fingerprint == "" {
		return SeedFingerprintMissing
	}
	for _, entry := range l.Entries {
		if entry.Key != key {
			continue
		}
		if entry.Fingerprint == fingerprint {
			return SeedReplayed
		}
		return SeedVersionMismatch
	}
	l.Entries = append(l.Entries, SeedLedgerEntry{Key: key, Fingerprint: fingerprint, CreatedAt: time.Now().UTC()})
	return SeedRecorded
}

// Lookup 返回键对应的账本记录；从未记录过时返回 false。
func (l *SeedLedger) Lookup(key SeedKey) (SeedLedgerEntry, bool) {
	if l == nil {
		return SeedLedgerEntry{}, false
	}
	for _, entry := range l.Entries {
		if entry.Key == key {
			return entry, true
		}
	}
	return SeedLedgerEntry{}, false
}

// AudioFingerprint 是音频文件内容的 sha256；容器头也参与计算，同一文件稳定、换文件必变。
func AudioFingerprint(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err = io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// seedLedgerFile 是种子账本文件名；与 outputs、voices 同级，属于整个数据目录。
const seedLedgerFile = "seed-ledger.json"

func seedLedgerPath(root string) string { return filepath.Join(root, seedLedgerFile) }

// ReadSeedLedger 读取账本；还没有账本时返回空账本，第一次生成从零开始。
func ReadSeedLedger(root string) (SeedLedger, error) {
	data, err := os.ReadFile(seedLedgerPath(root))
	if os.IsNotExist(err) {
		return SeedLedger{Entries: []SeedLedgerEntry{}}, nil
	}
	if err != nil {
		return SeedLedger{}, err
	}
	var ledger SeedLedger
	if err = json.Unmarshal(data, &ledger); err != nil {
		return SeedLedger{}, err
	}
	if ledger.Entries == nil {
		ledger.Entries = []SeedLedgerEntry{}
	}
	return ledger, nil
}

// WriteSeedLedger 原子写入账本：中断时不会留下半份 JSON。
func WriteSeedLedger(root string, ledger SeedLedger) error {
	if ledger.Entries == nil {
		ledger.Entries = []SeedLedgerEntry{}
	}
	data, err := json.MarshalIndent(ledger, "", "  ")
	if err != nil {
		return err
	}
	return platform.WriteFileAtomic(seedLedgerPath(root), append(data, '\n'), 0600)
}
