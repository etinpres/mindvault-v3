"""NEXT-31/33 alias generator — 1회성 batch로 각 메모리의 짧은 한국어 별칭 추출.

목적: hook 실시간 query rewriting 은 latency 800~3000ms 로 불가능했음 (NEXT-30.4
보류 사유). 대안으로 SessionEnd 직후 또는 수동 trigger 로 메모리당 5개 alias 를
미리 생성해 ~/.claude/mindvault-v3/alias_index.json 에 캐시 → memory_search.py
가 검색 시 latency 0 으로 lookup.

Provider:
- gemma  : 로컬 MLX 서버 (http://localhost:8080). 비용 0. alias 품질 보통
           (description 단어 그대로 쓰는 경향).
- claude : `claude` CLI subprocess 호출 (NEXT-33, 2026-05-24). MindVault 는
           Claude Code CLI 환경에서만 도는 도구라 사용자 인증은 이미 OAuth
           (Max/Pro 구독) 로 끝난 상태 — ANTHROPIC_API_KEY 요구 X. 구독 한도
           안에서 처리. alias 품질 우수 (description 우회 표현 등장).

활용: query 토큰들 중 어떤 메모리의 alias 와 매칭되면 해당 메모리 경로를
candidates 에 강제 추가 + score boost. 임베딩이 약한 케이스 ("프린터로" →
scanner-cli, "브이3" → project-mindvault) 회복용.

CLI:
    python -m alias_generator [--provider {gemma,claude}] [--model {sonnet,haiku}]
                              [--force] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
INDEX_PATH = DATA_DIR / "alias_index.json"
DEBUG_LOG = DATA_DIR / "debug.log"

GEMMA_URL = "http://localhost:8080/v1/chat/completions"
GEMMA_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
GEMMA_TIMEOUT = 30  # SessionEnd batch context — 여유

# NEXT-33: claude CLI 호출 시 schema 검증. structured_output 필드로 응답.
CLAUDE_TIMEOUT = 120  # cold start 첫 호출 15~30s, warm 15~25s. 여유 두기.
CLAUDE_SYSTEM_PROMPT = (
    "입력으로 한 메모리의 description + 본문 일부를 받는다. "
    "사용자가 그 메모리를 회수하려 할 때 입에서 나올 만한 한국어 우회 표현 5개를 alias 로 출력하라.\n"
    "규칙:\n"
    "- description / name 단어를 그대로 쓰지 말 것 (가장 중요)\n"
    "- 사용자 입에서 나올 법한 우회 표현·동의어·외래어·축약형·은어 위주\n"
    "- 각 alias 는 1~3 단어\n"
    "- 영문/숫자 약어가 사용자가 실제 쓸 만한 경우 포함 (\"v3\", \"msmtp\", \"Docker\" 등)\n"
    "- 잡담·맞장구·일반 명사 (\"도구\", \"시스템\", \"방법\", \"그거\") 절대 금지"
)
CLAUDE_SCHEMA = {
    "type": "object",
    "properties": {
        "aliases": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 5,
            "maxItems": 5,
        }
    },
    "required": ["aliases"],
    "additionalProperties": False,
}

# v3.2.6 H3: 하드코딩 2개 슬롯만으로는 NEXT-8 PROJECTS_ROOT 비대칭 dogfooding gap
# 이 alias 에도 재발 — cwd 별 projects 디렉토리가 자동 생성되므로 (Sprint 6) 모든
# slot 을 런타임에 자동 발견. .md 가 있는 활성 슬롯만 흡수.
# 환경변수 MV3_EXTRA_MEMORY_DIRS (콜론 구분) 로 명시 override 가능.
PROJECTS_ROOT = Path(os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser()


def discover_memory_dirs() -> list[Path]:
    dirs: list[Path] = []
    seen: set[str] = set()
    if PROJECTS_ROOT.is_dir():
        for child in sorted(PROJECTS_ROOT.iterdir()):
            mem = child / "memory"
            if not mem.is_dir():
                continue
            if not any(mem.glob("*.md")):
                continue
            key = str(mem.resolve())
            if key not in seen:
                seen.add(key)
                dirs.append(mem)
    extra = os.environ.get("MV3_EXTRA_MEMORY_DIRS", "")
    for raw in extra.split(":"):
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_dir():
            continue
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            dirs.append(path)
    return dirs


MEMORY_DIRS = discover_memory_dirs()

# Gemma 응답이 thinking trace 또는 JSON 잡음으로 새는 케이스 차단 위해 보수적 prompt.
_PROMPT = """\
다음은 한 메모리 파일의 description 과 본문 일부다. 사용자가 이 메모리를 회수하려
할 때 사용할 수 있는 짧은 한국어 별칭 5개를 줄바꿈으로만 출력해라.

규칙:
- 한 줄에 하나씩, 1~3 단어
- description 에 이미 명시된 표현 외에 사용자 입에서 나올 법한 우회 표현·동의어·축약형 위주
- 영문/숫자 약어가 합리적이면 포함 ("v3", "msmtp" 등)
- 잡담·맞장구·일반 명사 ("도구", "시스템" 등) 금지
- 부연 설명·번호·따옴표·thinking·해설 절대 금지
- 5줄만 출력하고 끝

메모리 description: {desc}

본문 일부:
{body}
"""


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] alias-gen: {msg}\n")
    except OSError:
        pass


def _call_gemma(desc: str, body: str) -> list[str]:
    prompt = _PROMPT.format(desc=desc[:300], body=body[:1500])
    payload = json.dumps({
        "model": GEMMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 120,
        "temperature": 0.1,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        GEMMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMMA_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        _debug(f"gemma call fail: {type(e).__name__} {e}")
        return []
    choices = data.get("choices") or []
    if not choices:
        return []
    msg = choices[0].get("message") or {}
    text = msg.get("content") or msg.get("reasoning") or ""
    return _parse_aliases(text)


def _call_claude(desc: str, body: str, model: str = "sonnet") -> list[str]:
    """NEXT-33 — claude CLI subprocess 호출.

    MindVault 는 Claude Code CLI 환경 도구라 사용자는 이미 OAuth (Max/Pro 구독)
    인증 끝난 상태. ANTHROPIC_API_KEY 요구하지 않음 — `claude` CLI 가 알아서
    OAuth 활용 → 구독 한도 안에서 처리. `--bare` 는 OAuth 안 읽으므로 X.

    --tools "" + --disable-slash-commands + --no-session-persistence 로 부작용
    최소화. --output-format json 의 응답 envelope 의 structured_output 필드에
    schema 매칭 결과가 들어옴.

    model: "sonnet" (claude-sonnet-4-6), "haiku" (claude-haiku-4-5)
    """
    user_prompt = f"description: {desc[:300]}\n\n본문 일부:\n{body[:1500]}"
    cmd = [
        "claude", "-p", user_prompt,
        "--model", model,
        "--system-prompt", CLAUDE_SYSTEM_PROMPT,
        "--output-format", "json",
        "--json-schema", json.dumps(CLAUDE_SCHEMA),
        "--tools", "",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            stdin=subprocess.DEVNULL,  # "no stdin" 경고 차단
        )
    except subprocess.TimeoutExpired:
        _debug(f"claude call timeout (>{CLAUDE_TIMEOUT}s)")
        return []
    except OSError as e:
        _debug(f"claude call OSError: {e}")
        return []
    if r.returncode != 0:
        _debug(f"claude exit={r.returncode} stderr={r.stderr[-200:]!r}")
        return []
    try:
        env = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        _debug(f"claude stdout JSONDecodeError: {e}")
        return []
    if env.get("is_error"):
        _debug(f"claude is_error: {env.get('result','')[:200]}")
        return []
    structured = env.get("structured_output") or {}
    aliases = structured.get("aliases") or []
    # claude CLI 는 schema minItems=5 검증을 통과한 결과만 반환하지만, 환경 변동
    # (rate limit / fallback) 으로 빈 응답 올 수 있으니 방어적으로 정리.
    cleaned: list[str] = []
    for a in aliases:
        a = str(a).strip().strip("\"'`")
        if a and len(a) <= 30:
            cleaned.append(a)
        if len(cleaned) >= 5:
            break
    return cleaned


def _parse_aliases(text: str) -> list[str]:
    """5줄 alias 추출 — 잡음·번호·따옴표 정리."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip().strip("\"'`")
        # 번호/대시/별표 같은 list marker 제거
        for marker in ("- ", "* ", "• "):
            if line.startswith(marker):
                line = line[len(marker):].strip()
        # "1. xxx" 같은 번호 prefix
        if len(line) > 2 and line[0].isdigit() and line[1] in (".", ")"):
            line = line[2:].strip()
        if not line or len(line) > 30:
            continue
        # description 잔재 prefix 자르기
        if line.lower().startswith(("alias", "별칭", "메모리", "description")):
            continue
        out.append(line)
        if len(out) >= 5:
            break
    return out


def _extract_memory_meta(md_path: Path) -> tuple[str, str, str] | None:
    """frontmatter name + description + 본문 첫 1500자.

    반환: (name, description, body_excerpt) 또는 None (frontmatter 형식 깨졌으면).
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    fm = parts[1]
    body = parts[2].strip()
    name = ""
    desc = ""
    for ln in fm.splitlines():
        if ln.startswith("name:"):
            name = ln[5:].strip()
        elif ln.startswith("description:"):
            desc = ln[12:].strip()
    if not name:
        return None
    return name, desc, body


def generate(
    force: bool = False,
    limit: int | None = None,
    provider: str = "gemma",
    model: str = "sonnet",
    purge_missing: bool = False,
) -> dict:
    """모든 메모리 .md → alias_index.json 갱신.

    provider: "gemma" (로컬 MLX, 비용 0, 품질 보통)
              "claude" (claude CLI subprocess, OAuth 인증 자동 활용, 품질 우수)
    model:    provider="claude" 일 때 "sonnet" | "haiku"
    force=False 면 이미 index 에 있는 path 는 skip (incremental).
    purge_missing=True 면 alias_index 안에서 디스크에 없는 path entry 를 제거 —
    SessionEnd 자동 동기화에서 dangling reference 누적 방지.
    """
    existing: dict[str, dict] = {}
    if INDEX_PATH.exists() and not force:
        try:
            existing = json.loads(INDEX_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    targets: list[Path] = []
    # v3.2.6 H3: 매 호출마다 재발견 — 새 cwd 슬롯이 생기면 즉시 흡수.
    for d in discover_memory_dirs():
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            # NEXT-34 #5 (2026-05-25): _staged 직속 파일도 명시 제외 (review 전
            # 메모리가 alias_index → recall 에 노출되는 leak 방지).
            if any(part == "_staged" for part in md.parts):
                continue
            targets.append(md)
        # _procedural/ 하위도 포함 (단, _procedural/_staged/ 는 제외).
        proc = d / "_procedural"
        if proc.is_dir():
            for md in sorted(proc.glob("*.md")):
                if any(part == "_staged" for part in md.parts):
                    continue
                targets.append(md)

    if limit is not None:
        targets = targets[:limit]

    purged = 0
    if purge_missing and existing:
        target_keys = {str(p) for p in targets}
        for k in list(existing.keys()):
            # 명시 제외 path (_staged, MEMORY.md 등) 도 alias_index 에서 함께 청소.
            kp = Path(k)
            is_excluded = any(part == "_staged" for part in kp.parts) or kp.name == "MEMORY.md"
            if k not in target_keys and (is_excluded or not kp.exists()):
                del existing[k]
                purged += 1

    stats = {
        "total": len(targets),
        "generated": 0,
        "skipped": 0,
        "failed": 0,
        "purged": purged,
        "provider": provider,
        "model": model if provider == "claude" else None,
    }
    t0 = time.time()
    for i, md in enumerate(targets):
        path_key = str(md)
        if path_key in existing and not force:
            stats["skipped"] += 1
            continue
        meta = _extract_memory_meta(md)
        if meta is None:
            stats["failed"] += 1
            continue
        name, desc, body = meta
        if provider == "claude":
            aliases = _call_claude(desc, body, model=model)
        else:
            aliases = _call_gemma(desc, body)
        if not aliases:
            stats["failed"] += 1
            _debug(f"no aliases ({provider}): {name}")
            continue
        existing[path_key] = {
            "name": name,
            "aliases": aliases,
            "provider": provider,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        stats["generated"] += 1
        # 매 10건마다 중간 저장 — 도중 실패해도 진행 보존
        if stats["generated"] % 10 == 0:
            _save(existing)
            print(f"  [{i+1}/{len(targets)}] {stats['generated']} ok ({(time.time()-t0):.0f}s)")
    _save(existing)
    stats["elapsed_s"] = round(time.time() - t0, 1)
    return stats


def _save(data: dict) -> None:
    """alias_index.json atomic write — tmp + os.replace() 로 partial write 차단.

    recall hook 의 load_alias_index() 가 동기적으로 읽는 도중 generate() 가
    write_text 중간에 crash 하면 부분 쓰인 파일이 JSONDecodeError 를 일으켜
    다음 SessionEnd 까지 alias boost 비활성. tmp 에 쓰고 atomic rename.
    """
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, INDEX_PATH)


def load_alias_index() -> dict:
    """memory_search.py 가 검색 시 호출. 캐시 없으면 빈 dict."""
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--provider",
        choices=["gemma", "claude"],
        default="gemma",
        help="alias 생성 provider. gemma=로컬 MLX (비용 0, 품질 보통). "
             "claude=`claude` CLI subprocess (OAuth 인증 자동, 구독 한도 안에서 처리, 품질 우수)",
    )
    p.add_argument(
        "--model",
        choices=["sonnet", "haiku"],
        default="sonnet",
        help="--provider claude 일 때 모델 선택. default=sonnet (claude-sonnet-4-6)",
    )
    p.add_argument("--force", action="store_true", help="기존 alias_index 전건 재생성")
    p.add_argument("--limit", type=int, default=None, help="최대 N건만 처리 (디버그)")
    p.add_argument(
        "--purge-missing",
        action="store_true",
        help="alias_index 안에서 디스크에 없는 path entry 제거 (dangling 정리)",
    )
    p.add_argument(
        "--sync",
        action="store_true",
        help="SessionEnd 자동 호출용 shortcut: --purge-missing 켠 incremental 동기화",
    )
    args = p.parse_args()
    if args.sync:
        args.purge_missing = True
    s = generate(
        force=args.force,
        limit=args.limit,
        provider=args.provider,
        model=args.model,
        purge_missing=args.purge_missing,
    )
    print(f"\nalias_index → {INDEX_PATH}")
    print(f"  provider={s['provider']}" + (f" model={s['model']}" if s['model'] else ""))
    print(
        f"  total={s['total']} generated={s['generated']} skipped={s['skipped']} "
        f"failed={s['failed']} purged={s.get('purged', 0)} elapsed={s['elapsed_s']}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
