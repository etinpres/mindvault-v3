#!/usr/bin/env bash
# MindVault v3.2.4 — Arctic-ko MLX 서버 launchd entry.
# v3.2.0 의 com.yonghaekim.arctic-ko-mlx plist 가 Python 3.10 절대경로 박혀
# 다른 사용자 환경에서 100% silent fail 했던 결함을 wrapper 로 일반화 (#1, #6).
# v3.2.4 fix-the-fix: install.sh 의 python3 와 wrapper PATH 의 첫 python3 가
# 일치 안 해서 mlx ImportError 가 났던 결함 — install.sh 가 자기 python3 의
# bin dir 을 __INSTALL_PYTHON_BIN__ placeholder 에 sed 치환 후 deploy.
#
# 환경변수:
#   MV3_RUNNER_DRY_RUN=1   실제 실행 안 하고 명령만 echo (테스트용)
set -euo pipefail

# launchd 환경의 좁은 PATH 보완 — pip --user 설치된 mlx_embeddings 찾기 위해.
# 맨 앞 __INSTALL_PYTHON_BIN__ 는 install.sh 가 deploy 시 자기 python3 의
# bin dir 로 치환 (사용자 환경에 따라 Apple framework Python · homebrew · uv ·
# pyenv 등 다양). 그 다음은 Gemma runner 와 동일 일반 PATH.
export PATH="__INSTALL_PYTHON_BIN__:$HOME/.local/bin:$HOME/Library/Python/3.10/bin:$HOME/Library/Python/3.11/bin:$HOME/Library/Python/3.12/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

SCRIPT="$HOME/.claude/scripts/mindvault/arctic_ko_server.py"

if [ ! -f "$SCRIPT" ]; then
  echo "FATAL: arctic_ko_server.py not found at $SCRIPT — run install.sh" >&2
  exit 1
fi

CMD=(python3 "$SCRIPT")

if [ "${MV3_RUNNER_DRY_RUN:-0}" = "1" ]; then
  echo "DRY_RUN: ${CMD[*]}"
  exit 0
fi

exec "${CMD[@]}"
