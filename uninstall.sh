#!/usr/bin/env bash
# MindVault v3 uninstaller — removes hooks, scripts, launchd services, and skill registrations.

set -euo pipefail

HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"

# Hook scripts targeted for removal. Matched as substring anywhere in the
# settings.json hook command (covers Stop / SessionEnd async variants too).
HOOK_TARGETS=(
  "$HOOKS_DIR/session-memory.py"
  "$HOOKS_DIR/session-memory-end.py"
  "$HOOKS_DIR/session-memory-end-async.sh"
  "$HOOKS_DIR/session-memory-precompute.sh"
  "$HOOKS_DIR/memory-recall.py"
)

# Launchd labels MindVault v3 owns. gemma-mlx is intentionally NOT in this list —
# it is shared infrastructure used outside mindvault.
LAUNCHD_LABELS=(
  "com.yonghaekim.arctic-ko-mlx"
  "com.yonghaekim.mv3-env"
  "com.yonghaekim.mv3-gemma-intent"
  "com.yonghaekim.mv3-stats-daily"
)

# --- 1. settings.json hook entries ----------------------------------------
if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$SETTINGS.bak"
  python3 - "$SETTINGS" "${HOOK_TARGETS[@]}" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
targets = sys.argv[2:]
data = json.loads(path.read_text()) if path.stat().st_size else {}
hooks = data.get("hooks", {})


def matches(cmd: str) -> bool:
    return any(t in (cmd or "") for t in targets)


removed = 0
for event_name in ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop"):
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

# --- 2. Hook scripts ------------------------------------------------------
for target in "${HOOK_TARGETS[@]}"; do
  if [ -f "$target" ]; then
    rm -f "$target"
    echo "✓ removed $target"
  fi
done

# --- 3. Slash command skills ----------------------------------------------
MEMORY_REVIEW_SKILL="$HOME/.claude/commands/memory_review.md"
if [ -f "$MEMORY_REVIEW_SKILL" ]; then
  rm -f "$MEMORY_REVIEW_SKILL"
  echo "✓ removed $MEMORY_REVIEW_SKILL"
fi

RECALL_SKILL="$HOME/.claude/commands/recall.md"
if [ -f "$RECALL_SKILL" ]; then
  rm -f "$RECALL_SKILL"
  echo "✓ removed $RECALL_SKILL"
fi

# /close-session + /cs alias 정리.
# audit-2026-05-25 N8 + round-3 D: 사용자 커스터마이즈 보존 위해 unique marker `[mv3-skill]` 매칭.
# (두 skill frontmatter description 첫머리에 박힌 고유 토큰. "MindVault v3" 같은 흔한 문구로는
# 사용자 personal 본문도 false-positive delete 가능했음.)
for skill in close-session cs; do
  target="$HOME/.claude/commands/${skill}.md"
  if [ -f "$target" ]; then
    if grep -qF '[mv3-skill]' "$target" 2>/dev/null; then
      rm -f "$target"
      echo "✓ removed $target"
    else
      echo "↷ skipped $target ([mv3-skill] marker 없음 — 사용자 변형으로 보임)"
    fi
  fi
done

# --- 4. Deployed scripts directory ----------------------------------------
SCRIPTS_DIR="$HOME/.claude/scripts/mindvault"
if [ -d "$SCRIPTS_DIR" ]; then
  rm -rf "$SCRIPTS_DIR"
  echo "✓ removed $SCRIPTS_DIR"
fi

# --- 5. Launchd services --------------------------------------------------
for label in "${LAUNCHD_LABELS[@]}"; do
  PLIST="$HOME/Library/LaunchAgents/${label}.plist"
  if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✓ removed launchd $label"
  fi
done

# --- 6. Optional: drop memories_* tables (--purge-vec) --------------------
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
echo "Arctic-ko model preserved at $HOME/.cache/mlx-arctic-ko/ (~322MB). Delete manually if desired."
echo "gemma-mlx launchd service preserved (shared infrastructure, not mindvault-owned)."
