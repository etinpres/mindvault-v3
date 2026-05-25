#!/usr/bin/env bash
# MindVault v3 installer — deploys SessionStart hook to ~/.claude/.
# Idempotent; safe to re-run. Creates settings.json.bak before edit.

set -euo pipefail

# v3.2.0 Task 1 — Apple Silicon 가드 + 헬퍼.
# ARCH_OVERRIDE 환경변수는 테스트 전용 (tests/test_install_v320.py).
_ARCH="${ARCH_OVERRIDE:-$(uname -m)}"
_OS="$(uname -s)"
if [ "$_ARCH" != "arm64" ] || [ "$_OS" != "Darwin" ]; then
  echo "⚠ MindVault v3 의 MLX 백엔드는 Apple Silicon Mac 에서만 동작합니다."
  echo "  현재 환경: $_OS $_ARCH"
  echo "  Linux/Intel Mac 지원은 v3.3.0 (백엔드 추상화) 예정."
  read -r -p "  계속 진행 시 모델 자동 설치(Sprint 4.5/17)는 건너뜁니다. 인프라만 설치하시겠습니까? [y/N] " _resp
  if [ "${_resp:-N}" != "y" ] && [ "${_resp:-N}" != "Y" ]; then
    echo "  설치 취소."
    exit 1
  fi
  export MV3_SKIP_MODELS=1
fi

if [ "${MV3_GUARD_ONLY:-0}" = "1" ]; then
  exit 0
fi

# v3.2.0 Task 1 — checkpoint 헬퍼.
# do_step <name> <step_file> <action_command>
do_step() {
  local name="$1" step_file="$2" action="$3"
  if grep -q "^${name}$" "$step_file" 2>/dev/null; then
    echo "  ✓ ${name} (already done, skipping)"
    return 0
  fi
  echo "→ ${name} ..."
  if eval "$action"; then
    mkdir -p "$(dirname "$step_file")"
    echo "$name" >> "$step_file"
    echo "  ✓ ${name}"
    return 0
  else
    echo "  ✗ ${name} FAILED"
    print_next_step "$name"
    return 1
  fi
}

print_next_step() {
  case "$1" in
    deps-ok)      echo "  Next: pip 환경 확인 (python3 -m pip --version) 후 ./install.sh 재실행" ;;
    downloaded)   echo "  Next: 네트워크 확인 후 ./install.sh 재실행 (huggingface_hub 가 partial 캐시 자동 활용)" ;;
    converted)    echo "  Next: 디스크 1.5GB 확보 후 ./install.sh 재실행" ;;
    plist-loaded) echo "  Next: ls -la ~/Library/LaunchAgents/ 권한 확인 후 ./install.sh 재실행" ;;
    healthy)      echo "  Next: tail ~/Library/Logs/{gemma,arctic-ko}-mlx.err 진단 후 ./install.sh 재실행" ;;
    verified)     echo "  Next: ls ~/.cache/mlx-arctic-ko/model.safetensors 확인 후 ./install.sh 재실행" ;;
  esac
}

if [ "${MV3_SOURCE_HELPERS_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO_DIR/src/session_memory.py"
HOOKS_DIR="$HOME/.claude/hooks"
TARGET="$HOOKS_DIR/session-memory.py"
SETTINGS="$HOME/.claude/settings.json"
# 훅 스크립트 shebang(#!/usr/bin/env python3)과 chmod +x에 의존.
HOOK_CMD="$TARGET"

# Sprint 2 추가 자산
SCRIPTS_DIR="$HOME/.claude/scripts/mindvault"
COMMANDS_DIR="$HOME/.claude/commands"
SPRINT2_SRC=("$REPO_DIR/src/indexer.py" "$REPO_DIR/src/search.py" "$REPO_DIR/src/recall_cli.py")
RECALL_SKILL_SRC="$REPO_DIR/skill/recall.md"
RECALL_SKILL_TARGET="$COMMANDS_DIR/recall.md"

# Sprint 3 추가 자산
END_SRC="$REPO_DIR/src/session_memory_end.py"
END_TARGET="$HOOKS_DIR/session-memory-end.py"
END_CMD="$END_TARGET"
# NEXT-23/24 (2026-05-24): async wrapper (macOS 호환 nohup+&+disown).
# wrapper 가 SessionEnd hook 를 백그라운드로 detach — Claude Code 종료 시 SIGTERM 회피.
# setsid 사용한 옛 버전은 macOS 에 setsid 없어서 첫 줄 fail.
END_WRAPPER_SRC="$REPO_DIR/hooks/session-memory-end-async.sh"
END_WRAPPER_TARGET="$HOOKS_DIR/session-memory-end-async.sh"
SPRINT3_SRC=("$REPO_DIR/src/memory_extractor.py" "$REPO_DIR/src/memory_review_cli.py")
MEMORY_SKILL_SRC="$REPO_DIR/skill/memory_review.md"
MEMORY_SKILL_TARGET="$COMMANDS_DIR/memory_review.md"

# close-session 명시 closer (자동 hook narrative 보완용)
CLOSE_SESSION_SKILL_SRC="$REPO_DIR/skill/close-session.md"
CLOSE_SESSION_SKILL_TARGET="$COMMANDS_DIR/close-session.md"
CS_SKILL_SRC="$REPO_DIR/skill/cs.md"
CS_SKILL_TARGET="$COMMANDS_DIR/cs.md"

if [ ! -f "$SRC" ]; then
  echo "error: $SRC not found" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not on PATH" >&2
  exit 1
fi

mkdir -p "$HOOKS_DIR" "$HOME/.claude/mindvault-v3/cache" "$SCRIPTS_DIR" "$COMMANDS_DIR"
cp "$SRC" "$TARGET"
chmod +x "$TARGET"
echo "✓ copied hook to $TARGET"

# Sprint 2: scripts/mindvault/ 배포
for f in "${SPRINT2_SRC[@]}"; do
  if [ -f "$f" ]; then
    cp "$f" "$SCRIPTS_DIR/$(basename "$f")"
    chmod +x "$SCRIPTS_DIR/$(basename "$f")"
  fi
done
echo "✓ deployed Sprint 2 scripts to $SCRIPTS_DIR"

# Sprint 2: /recall 스킬 배포
if [ -f "$RECALL_SKILL_SRC" ]; then
  if [ -f "$RECALL_SKILL_TARGET" ]; then
    cp "$RECALL_SKILL_TARGET" "$RECALL_SKILL_TARGET.bak"
  fi
  cp "$RECALL_SKILL_SRC" "$RECALL_SKILL_TARGET"
  echo "✓ installed /recall skill at $RECALL_SKILL_TARGET"
fi

# Sprint 3: SessionEnd 훅 + 추가 스크립트 + /memory review 스킬
if [ -f "$END_SRC" ]; then
  cp "$END_SRC" "$END_TARGET"
  chmod +x "$END_TARGET"
  echo "✓ copied SessionEnd hook to $END_TARGET"
fi
# NEXT-24: async wrapper sync. wrapper 가 깨지면 Claude Code 가 hook subprocess
# 강제 종료 → Gemma 호출 도중 SIGTERM → staged 안 됨. install 재실행 시 자동 회복.
if [ -f "$END_WRAPPER_SRC" ]; then
  cp "$END_WRAPPER_SRC" "$END_WRAPPER_TARGET"
  chmod +x "$END_WRAPPER_TARGET"
  echo "✓ copied SessionEnd async wrapper to $END_WRAPPER_TARGET"
fi
for f in "${SPRINT3_SRC[@]}"; do
  if [ -f "$f" ]; then
    cp "$f" "$SCRIPTS_DIR/$(basename "$f")"
    chmod +x "$SCRIPTS_DIR/$(basename "$f")"
  fi
done
echo "✓ deployed Sprint 3 scripts to $SCRIPTS_DIR"
if [ -f "$MEMORY_SKILL_SRC" ]; then
  if [ -f "$MEMORY_SKILL_TARGET" ]; then
    cp "$MEMORY_SKILL_TARGET" "$MEMORY_SKILL_TARGET.bak"
  fi
  cp "$MEMORY_SKILL_SRC" "$MEMORY_SKILL_TARGET"
  echo "✓ installed /memory_review skill at $MEMORY_SKILL_TARGET"
fi

# /close-session + /cs alias 배포 (자동 hook 의 narrative 보완용 명시 closer)
# 인덱스 0,3,6: src / target / label triple (colon-delim 회피 — path 안 ':' 안전)
SKILL_TRIPLES=(
  "$CLOSE_SESSION_SKILL_SRC" "$CLOSE_SESSION_SKILL_TARGET" "/close-session"
  "$CS_SKILL_SRC"            "$CS_SKILL_TARGET"            "/cs"
)
i=0
while [ $i -lt ${#SKILL_TRIPLES[@]} ]; do
  src="${SKILL_TRIPLES[$i]}"
  target="${SKILL_TRIPLES[$((i+1))]}"
  label="${SKILL_TRIPLES[$((i+2))]}"
  if [ -f "$src" ]; then
    [ -f "$target" ] && cp "$target" "$target.bak"
    cp "$src" "$target"
    echo "✓ installed $label skill at $target"
  fi
  i=$((i+3))
done

# v3.1.1 (audit-2026-05-25 post-ship CRITICAL): 옛 personal SKILL 디렉토리
# `~/.claude/skills/{close-session,cs}/` 가 새 deploy 본을 가릴 수 있음.
# round-6 A,B 가드 추가: SKILL.md 부재 시 corrupt install 로 간주 → 보존,
# backup 디렉토리에 PID 추가 (같은 초 parallel install collision 차단).
PERSONAL_SKILLS_BACKUP="$HOME/.claude/skills.attic/mv3-skill-conflict-$(date +%Y%m%d-%H%M%S)-$$"
for personal in "$HOME/.claude/skills/close-session" "$HOME/.claude/skills/cs"; do
  if [ -d "$personal" ]; then
    skill_md="$personal/SKILL.md"
    if [ ! -f "$skill_md" ]; then
      # round-6 A: SKILL.md 없는 corrupt/empty 디렉토리 — 사용자 의도 불명, 보존
      echo "↷ $personal 는 SKILL.md 없음 (corrupt 가능) — 보존, 수동 정리 권장"
    elif grep -qF '[mv3-skill]' "$skill_md" 2>/dev/null; then
      # 이미 v3 deploy 본의 변형 — 그대로 두고 새 본이 commands/ 에서 작동
      echo "↷ $personal 는 v3 변형 (sentinel ✓) — 보존, $CLOSE_SESSION_SKILL_TARGET 우선 매칭"
    else
      # 사용자 personal — 백업 후 제거
      mkdir -p "$PERSONAL_SKILLS_BACKUP"
      mv "$personal" "$PERSONAL_SKILLS_BACKUP/"
      echo "⚠️  $personal 를 $PERSONAL_SKILLS_BACKUP/ 로 백업 (옛 personal SKILL 이 v3 본을 가리지 않도록)"
    fi
  fi
done

if [ ! -f "$SETTINGS" ]; then
  echo '{"hooks":{}}' > "$SETTINGS"
  echo "✓ created $SETTINGS"
fi

cp "$SETTINGS" "$SETTINGS.bak"
echo "✓ backup at $SETTINGS.bak"

python3 - "$SETTINGS" "$HOOK_CMD" "$TARGET" "$END_WRAPPER_TARGET" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
start_cmd = sys.argv[2]
start_target = sys.argv[3]
end_wrapper_cmd = sys.argv[4]
data = json.loads(path.read_text()) if path.stat().st_size else {}
hooks = data.setdefault("hooks", {})


def register(event_name, cmd, match_targets):
    """match_targets 의 어떤 substring 라도 매칭되는 기존 hook 모두 cleanup 후 cmd 단일 등록.

    NEXT-25: 옛 SessionEnd 가 wrapper(.sh) + 직접 py 두 hook 동시 등록 →
    매 fire 마다 always-fire bypass 두 번 + cache race. 둘 다 cleanup 후 wrapper 단일.
    """
    events = hooks.setdefault(event_name, [])
    cleaned = 0
    kept_events = []
    for entry in events:
        kept = []
        for h in entry.get("hooks", []):
            command = h.get("command") or ""
            if any(t in command for t in match_targets):
                cleaned += 1
                continue
            kept.append(h)
        if kept:
            entry["hooks"] = kept
            kept_events.append(entry)
    if cleaned:
        print(f"  removed {cleaned} stale MindVault entries from {event_name}")
    kept_events.append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": cmd}],
    })
    hooks[event_name] = kept_events
    print(f"✓ registered {event_name} hook")


register("SessionStart", start_cmd, [start_target])
# NEXT-25: SessionEnd 는 wrapper 만 단일 등록 — 옛 직접 py path 도 같이 cleanup.
register("SessionEnd", end_wrapper_cmd,
         ["session-memory-end.py", "session-memory-end-async.sh"])
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
PY

echo ""
echo "→ Pre-warming Gemma cache (first-session 지연 방지, 최대 45초)..."
if "$TARGET" </dev/null >/dev/null 2>&1; then
  echo "✓ pre-warm complete"
else
  echo "  (pre-warm skipped: Gemma 서버 미응답. 첫 세션에서 실제 요약 시도됨)"
fi

echo ""
echo "→ Building FTS5 search index (Sprint 2)..."
if python3 "$SCRIPTS_DIR/indexer.py" >/dev/null 2>&1; then
  cnt=$(sqlite3 "$HOME/.claude/mindvault-v3/index.db" "SELECT COUNT(*) FROM sessions" 2>/dev/null || echo "?")
  echo "✓ indexed $cnt sessions"
else
  echo "  (index build skipped)"
fi

echo ""
echo ""
echo "── Sprint 4 — Layer 4 Memory Recall (Arctic-ko hybrid RRF) ─────────────────────"

# Sprint 4 추가 자산
ARCTIC_SERVER_SRC="$REPO_DIR/scripts/arctic_ko_server.py"
ARCTIC_SERVER_TARGET="$SCRIPTS_DIR/arctic_ko_server.py"
ARCTIC_PLIST_SRC="$REPO_DIR/plist/com.yonghaekim.arctic-ko-mlx.plist"
ARCTIC_PLIST_TARGET="$HOME/Library/LaunchAgents/com.yonghaekim.arctic-ko-mlx.plist"
MEMORY_HOOK_SRC="$REPO_DIR/hooks/memory-recall.py"
MEMORY_HOOK_TARGET="$HOOKS_DIR/memory-recall.py"
SPRINT4_SRC=("$REPO_DIR/src/memory_indexer.py" "$REPO_DIR/src/memory_search.py")
ARCTIC_MODEL_DIR="$HOME/.cache/mlx-arctic-ko"

# 4.1 Python 의존성
echo "→ Installing Python dependencies (sqlite-vec mlx-embeddings pyyaml numpy huggingface_hub)..."
if python3 -m pip install --user --quiet sqlite-vec mlx-embeddings pyyaml numpy huggingface_hub 2>&1 | tail -3; then
  echo "✓ dependencies installed"
else
  echo "  (warning: dependency install had warnings — Sprint 4 may not work)"
fi

# 4.2 Arctic-ko MLX 4bit 모델 (수동 변환 필요)
# Sprint 9: dragonkue/snowflake-arctic-embed-l-v2.0-ko 원본을 MLX 4bit 양자화한
# 로컬 모델 사용. mlx-community에 4bit 양자화본 미존재 — 사용자 직접 변환 필요.
ARCTIC_MODEL_READY=0
if [ -f "$ARCTIC_MODEL_DIR/model.safetensors" ]; then
  echo "✓ Arctic-ko model already present at $ARCTIC_MODEL_DIR"
  ARCTIC_MODEL_READY=1
else
  echo ""
  echo "  ⚠ Arctic-ko MLX 4bit 모델이 $ARCTIC_MODEL_DIR 에 없습니다."
  echo "  수동 변환 절차 (1회만 필요):"
  echo "    1) pip install --user mlx_embeddings huggingface_hub"
  echo "    2) python3 -c \"from mlx_embeddings.utils import convert; convert('dragonkue/snowflake-arctic-embed-l-v2.0-ko', mlx_path='$ARCTIC_MODEL_DIR', quantize=True, q_bits=4)\""
  echo "    3) ls $ARCTIC_MODEL_DIR/model.safetensors  # 확인"
  echo "    4) 본 installer 재실행"
  echo "  자세한 안내: README.md 의 'Arctic-ko 모델 변환' 섹션."
  echo "  (모델 없으면 memory-recall hook 은 silent no-op)"
  echo ""
fi

# 4.3 스크립트 + 서버 배포
for f in "${SPRINT4_SRC[@]}"; do
  if [ -f "$f" ]; then
    cp "$f" "$SCRIPTS_DIR/$(basename "$f")"
    chmod +x "$SCRIPTS_DIR/$(basename "$f")"
  fi
done
if [ -f "$ARCTIC_SERVER_SRC" ]; then
  cp "$ARCTIC_SERVER_SRC" "$ARCTIC_SERVER_TARGET"
  chmod +x "$ARCTIC_SERVER_TARGET"
fi
echo "✓ deployed Sprint 4 scripts to $SCRIPTS_DIR"

# Sprint 4: memory_review_cli도 reindex 트리거 포함된 새 버전으로 재배포
if [ -f "$REPO_DIR/src/memory_review_cli.py" ]; then
  cp "$REPO_DIR/src/memory_review_cli.py" "$SCRIPTS_DIR/memory_review_cli.py"
  chmod +x "$SCRIPTS_DIR/memory_review_cli.py"
fi

# post-ship: 런타임 import dependencies — 이전 install.sh 가 명시 누락한 채
# 옛 매뉴얼 cp 로만 production 에 존재하던 파일들. 신규 환경에서 install.sh
# 만 실행하면 hook 이 ImportError 로 silent fail 했음.
#
# 분류:
#  - hook 직접 import: query_intent (memory-recall.py)
#  - extractor 체인: extractor_cache, memory_compiler (session-memory-end)
#  - turn 분할 캐시: turns_cache (recall_cli / search)
#  - Sprint 16+: sources_cli (영구 source 등록), backfill_cli (vec 백필),
#                dedup_cli (중복 정리), extractor_stats_cli (관측)
#  - NEXT-31~33: alias_generator (alias_index 자산 생성)
RUNTIME_EXTRA_SRC=(
  "$REPO_DIR/src/query_intent.py"
  "$REPO_DIR/src/extractor_cache.py"
  "$REPO_DIR/src/memory_compiler.py"
  "$REPO_DIR/src/turns_cache.py"
  "$REPO_DIR/src/sources_cli.py"
  "$REPO_DIR/src/backfill_cli.py"
  "$REPO_DIR/src/dedup_cli.py"
  "$REPO_DIR/src/extractor_stats_cli.py"
  "$REPO_DIR/src/alias_generator.py"
)
for f in "${RUNTIME_EXTRA_SRC[@]}"; do
  if [ -f "$f" ]; then
    cp "$f" "$SCRIPTS_DIR/$(basename "$f")"
    chmod +x "$SCRIPTS_DIR/$(basename "$f")"
  fi
done
echo "✓ deployed runtime extras ($(echo "${RUNTIME_EXTRA_SRC[@]}" | wc -w | tr -d ' ') files) to $SCRIPTS_DIR"

# 4.4a 옛 BGE-M3 plist migration (Sprint 9 이전 설치자 → Arctic-ko 전환)
OLD_BGE_PLIST="$HOME/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist"
if [ -f "$OLD_BGE_PLIST" ]; then
  launchctl unload "$OLD_BGE_PLIST" >/dev/null 2>&1 || true
  rm -f "$OLD_BGE_PLIST"
  echo "✓ migrated: removed legacy BGE-M3 plist ($OLD_BGE_PLIST)"
fi

# 4.4 Arctic-ko launchd plist
# 템플릿 placeholder(__USER_HOME__) 를 현재 $HOME 으로 치환한 뒤 설치.
# sed delimiter 로 `|` 사용 (path 에 `/` 가 들어가므로).
if [ -f "$ARCTIC_PLIST_SRC" ]; then
  sed "s|__USER_HOME__|$HOME|g" "$ARCTIC_PLIST_SRC" > "$ARCTIC_PLIST_TARGET"
  launchctl unload "$ARCTIC_PLIST_TARGET" >/dev/null 2>&1 || true
  launchctl load -w "$ARCTIC_PLIST_TARGET" 2>/dev/null || true
  echo "✓ Arctic-ko launchd service loaded (port 8081)"
fi

# 4.5 hook 배포
if [ -f "$MEMORY_HOOK_SRC" ]; then
  cp "$MEMORY_HOOK_SRC" "$MEMORY_HOOK_TARGET"
  chmod +x "$MEMORY_HOOK_TARGET"
  echo "✓ memory-recall hook at $MEMORY_HOOK_TARGET"
fi

# 4.6 헬스체크 (Arctic-ko 모델 로딩 대기 ~10초)
echo "→ Waiting for Arctic-ko to load (up to 30s)..."
HEALTH_OK=0
for i in $(seq 1 15); do
  if curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/health 2>/dev/null | grep -q "200"; then
    HEALTH_OK=1
    break
  fi
  sleep 2
done
if [ "$HEALTH_OK" = "1" ]; then
  DIM=$(curl -sS http://127.0.0.1:8081/health | python3 -c "import json,sys; print(json.load(sys.stdin)['dim'])" 2>/dev/null || echo "?")
  echo "✓ Arctic-ko health: OK (dim=$DIM)"
else
  echo "  ✗ Arctic-ko health check failed — hook will silently no-op"
  echo "  diagnose: tail ~/Library/Logs/arctic-ko-mlx.err"
fi

# 4.7 settings.json UserPromptSubmit hook 등록 (idempotent, 기존 hook 보존)
python3 - "$MEMORY_HOOK_TARGET" "$SETTINGS" <<'PY'
import json, sys
from pathlib import Path

hook_cmd = sys.argv[1]
settings_path = Path(sys.argv[2])
data = json.loads(settings_path.read_text())
hooks = data.setdefault("hooks", {})
ups = hooks.setdefault("UserPromptSubmit", [])

new_hook = {
    "matcher": "*",
    "hooks": [{"type": "command", "command": hook_cmd}],
}
# 동일 hook 이미 있으면 skip
already = any("memory-recall.py" in json.dumps(h) for h in ups)
if not already:
    ups.append(new_hook)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print("✓ registered UserPromptSubmit hook (memory-recall)")
else:
    print("✓ UserPromptSubmit hook already present — skip")
PY

# 4.8 초기 인덱싱 (모델 + health 둘 다 확보돼야 진행)
if [ "$HEALTH_OK" = "1" ] && [ "$ARCTIC_MODEL_READY" = "1" ]; then
  echo "→ Initial memory indexing (may take ~30-60s for ~100 memories)..."
  if python3 -c "
import sys
sys.path.insert(0, '$SCRIPTS_DIR')
from memory_indexer import full_rebuild
n = full_rebuild()
print(f'✓ indexed {n} memories')
" 2>&1 | tail -3; then
    :
  else
    echo "  (initial indexing skipped — run later with: python3 $SCRIPTS_DIR/memory_indexer.py)"
  fi

  # 4.9 hook warmup — cold-start latency 미리 지불 (200ms→150ms)
  echo "→ Pre-warming hook (cold start mitigation)..."
  echo '{"prompt":"warmup"}' | python3 "$MEMORY_HOOK_TARGET" >/dev/null 2>&1 || true
  echo "✓ hook pre-warmed"
fi

echo ""
echo "Installation complete. Start a new Claude Code session to verify."
echo "Try: /recall <검색어>"
echo "Uninstall: ./uninstall.sh"
