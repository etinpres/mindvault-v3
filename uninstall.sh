#!/usr/bin/env bash
# MindVault v3 uninstaller — removes SessionStart hook and the script.

set -euo pipefail

HOOKS_DIR="$HOME/.claude/hooks"
TARGET="$HOOKS_DIR/session-memory.py"
END_TARGET="$HOOKS_DIR/session-memory-end.py"
MEMORY_HOOK_TARGET="$HOOKS_DIR/memory-recall.py"
SETTINGS="$HOME/.claude/settings.json"
HOOK_CMD="$TARGET"

if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$SETTINGS.bak"
  python3 - "$SETTINGS" "$TARGET" "$END_TARGET" "$MEMORY_HOOK_TARGET" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
targets = [sys.argv[2], sys.argv[3], sys.argv[4]]
data = json.loads(path.read_text()) if path.stat().st_size else {}
hooks = data.get("hooks", {})


def matches(cmd: str) -> bool:
    return any(t in (cmd or "") for t in targets)


removed = 0
for event_name in ("SessionStart", "SessionEnd", "UserPromptSubmit"):
    events = hooks.get(event_name, [])
    new_events = []
    for entry in events:
        kept = [h for h in entry.get("hooks", []) if not matches(h.get("command", ""))]
        removed += len(entry.get("hooks", [])) - len(kept)
        if kept:
            entry["hooks"] = kept
            new_events.append(entry)
    if new_events:
        hooks[event_name] = new_events
    elif event_name in hooks:
        del hooks[event_name]

path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
print(f"✓ removed {removed} hook entries from settings.json")
PY
fi

if [ -f "$TARGET" ]; then
  rm -f "$TARGET"
  echo "✓ removed $TARGET"
fi

if [ -f "$END_TARGET" ]; then
  rm -f "$END_TARGET"
  echo "✓ removed $END_TARGET"
fi

# Sprint 3: /memory_review 스킬 제거
MEMORY_REVIEW_SKILL="$HOME/.claude/commands/memory_review.md"
if [ -f "$MEMORY_REVIEW_SKILL" ]; then
  rm -f "$MEMORY_REVIEW_SKILL"
  echo "✓ removed $MEMORY_REVIEW_SKILL"
fi

# Sprint 2: scripts/mindvault/ 제거
SCRIPTS_DIR="$HOME/.claude/scripts/mindvault"
if [ -d "$SCRIPTS_DIR" ]; then
  rm -rf "$SCRIPTS_DIR"
  echo "✓ removed $SCRIPTS_DIR"
fi

# Sprint 2: /recall 스킬 제거
RECALL_SKILL="$HOME/.claude/commands/recall.md"
if [ -f "$RECALL_SKILL" ]; then
  rm -f "$RECALL_SKILL"
  echo "✓ removed $RECALL_SKILL"
fi

# Sprint 4: memory-recall hook 제거
if [ -f "$MEMORY_HOOK_TARGET" ]; then
  rm -f "$MEMORY_HOOK_TARGET"
  echo "✓ removed $MEMORY_HOOK_TARGET"
fi

# Sprint 4: BGE-M3 launchd 서비스 제거
BGE_PLIST_TARGET="$HOME/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist"
if [ -f "$BGE_PLIST_TARGET" ]; then
  launchctl unload "$BGE_PLIST_TARGET" 2>/dev/null || true
  rm -f "$BGE_PLIST_TARGET"
  echo "✓ removed BGE-M3 launchd service"
fi

# Sprint 4: memories_* 테이블 옵션 제거 (--purge-vec 플래그)
if [ "${1:-}" = "--purge-vec" ]; then
  if [ -f "$HOME/.claude/mindvault-v3/index.db" ]; then
    sqlite3 "$HOME/.claude/mindvault-v3/index.db" \
      "DROP TABLE IF EXISTS memories_vec; DROP TABLE IF EXISTS memories_fts; DROP TABLE IF EXISTS memories;" 2>/dev/null \
      && echo "✓ dropped memories_* tables (--purge-vec)"
  fi
fi

echo ""
echo "Uninstall complete."
echo "Cache + index preserved at $HOME/.claude/mindvault-v3/. Delete manually if desired."
echo "BGE-M3 model preserved at $HOME/.cache/mlx-bge-m3/ (~322MB). Delete manually if desired."
