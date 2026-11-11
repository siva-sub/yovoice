package workbench

import (
	"os"
	"path/filepath"
	"testing"
)

// 同一组参数与同一指纹重放通过；指纹变化必须标版本不符，并保留第一次的指纹。
func TestSeedLedgerReplay(t *testing.T) {
	key := SeedKey{
		ModelID:     "breeze-tts-2",
		AnchorRev:   "anchor-sha",
		TextHash:    TextHash("夜色很深。"),
		Instruction: "低哑、克制",
		CFG:         2.5,
		Seed:        20261111,
	}
	ledger := SeedLedger{}
	if status := ledger.Record(key, "audio-sha-1"); status != SeedRecorded {
		t.Fatalf("第一次应先记录：%s", status)
	}
	if status := ledger.Record(key, "audio-sha-1"); status != SeedReplayed {
		t.Fatalf("同键同指纹应通过：%s", status)
	}
	if status := ledger.Record(key, "audio-sha-2"); status != SeedVersionMismatch {
		t.Fatalf("同键不同指纹应标版本不符：%s", status)
	}
	entry, ok := ledger.Lookup(key)
	if !ok || entry.Fingerprint != "audio-sha-1" {
		t.Fatalf("账本应保留第一次的指纹：%+v %v", entry, ok)
	}
	if status := ledger.Record(key, ""); status != SeedFingerprintMissing {
		t.Fatalf("没有指纹时不应入账：%s", status)
	}
}

// 键里任何一项不同都视为另一条记录，不会互相判定版本不符。
func TestSeedLedgerSeparatesKeys(t *testing.T) {
	base := SeedKey{ModelID: "m", TextHash: TextHash("t"), CFG: 2, Seed: 1}
	ledger := SeedLedger{}
	if status := ledger.Record(base, "one"); status != SeedRecorded {
		t.Fatal(status)
	}
	other := base
	other.Seed = 2
	if status := ledger.Record(other, "two"); status != SeedRecorded {
		t.Fatalf("不同种子应是新记录：%s", status)
	}
	if len(ledger.Entries) != 2 {
		t.Fatalf("两条不同键都应保留：%+v", ledger.Entries)
	}
}

// 账本写到数据目录后能原样读回；音频指纹按文件内容区分。
func TestSeedLedgerRoundTripAndAudioFingerprint(t *testing.T) {
	root := t.TempDir()
	ledger := SeedLedger{}
	key := SeedKey{ModelID: "m", TextHash: TextHash("t"), CFG: 1, Seed: 9}
	ledger.Record(key, "fingerprint")
	if err := WriteSeedLedger(root, ledger); err != nil {
		t.Fatal(err)
	}
	loaded, err := ReadSeedLedger(root)
	if err != nil {
		t.Fatal(err)
	}
	if entry, ok := loaded.Lookup(key); !ok || entry.Fingerprint != "fingerprint" {
		t.Fatalf("读回的账本缺少记录：%+v %v", entry, ok)
	}
	if empty, err := ReadSeedLedger(t.TempDir()); err != nil || len(empty.Entries) != 0 {
		t.Fatalf("没有账本时应返回空账本：%+v %v", empty, err)
	}

	first, second := filepath.Join(root, "a.wav"), filepath.Join(root, "b.wav")
	if err = os.WriteFile(first, []byte("audio-a"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(second, []byte("audio-b"), 0600); err != nil {
		t.Fatal(err)
	}
	a, err := AudioFingerprint(first)
	if err != nil {
		t.Fatal(err)
	}
	again, err := AudioFingerprint(first)
	if err != nil || again != a {
		t.Fatalf("同一文件指纹应稳定：%s %s %v", a, again, err)
	}
	b, err := AudioFingerprint(second)
	if err != nil || b == a {
		t.Fatalf("不同内容应有不同指纹：%s %s %v", a, b, err)
	}
}

// cue 的种子为空时不能生成可重放的账本键。
func TestSeedKeyForCueRequiresSeed(t *testing.T) {
	if _, ok := SeedKeyForCue(RenderCue{ModelID: "m", Text: "t"}); ok {
		t.Fatal("自动种子不应生成账本键")
	}
	seed := 42
	key, ok := SeedKeyForCue(RenderCue{ModelID: "m", AnchorRev: "a", Text: "t", Instruction: "i", CFG: 3, Seed: &seed})
	if !ok || key.Seed != 42 || key.TextHash != TextHash("t") || key.CFG != 3 {
		t.Fatalf("账本键应带上渲染参数：%+v %v", key, ok)
	}
}
