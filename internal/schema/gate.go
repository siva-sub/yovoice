package schema

import (
	"crypto/sha256"
	"encoding/hex"
)

// 闸门的四个状态。read 表示这一版正文被读并取过指纹，attributed 表示为这段正文记过说话人映射，
// verified 表示带着归因通读确认；只有 verified 允许渲染（ADR-0001）。
const (
	GatePending    = "pending"
	GateRead       = "read"
	GateAttributed = "attributed"
	GateVerified   = "verified"
)

// ChapterStatus 返回章节的闸门状态；没有闸门的旧作品按契约视为 pending。
func (d Draft) ChapterStatus() string {
	if d.Gate == nil {
		return GatePending
	}
	return d.Gate.Status
}

// TextSHA 是章节正文的指纹：sha256 的十六进制前 16 位。正文变化后闸门失效，由改写正文的一方清空 Gate。
func TextSHA(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])[:16]
}
