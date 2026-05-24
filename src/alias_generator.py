"""NEXT-31 alias generator — Gemma 1회성 batch로 각 메모리의 짧은 한국어 별칭 추출.

목적: hook 실시간 query rewriting 은 latency 800~3000ms 로 불가능했음 (NEXT-30.4
보류 사유). 대안으로 SessionEnd 직후 또는 수동 trigger 로 메모리당 5개 alias 를
미리 생성해 ~/.claude/mindvault-v3/alias_index.json 에 캐시 → memory_search.py
가 검색 시 latency 0 으로 lookup.

활용: query 토큰들 중 어떤 메모리의 alias 와 매칭되면 해당 메모리 경로를
candidates 에 강제 추가 + score boost. 임베딩이 약한 케이스 ("프린터로" →
scanner-cli, "브이3" → project-mindvault) 회복용.

CLI:
    python -m alias_generator [--force] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path("~/.claude/mindvault-v3").expanduser()
INDEX_PATH = DATA_DIR / "alias_index.json"
DEBUG_LOG = DATA_DIR / "debug.log"

GEMMA_URL = "http://localhost:8080/v1/chat/completions"
GEMMA_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
GEMMA_TIMEOUT = 30  # SessionEnd batch context — 여유

MEMORY_DIRS = [
    Path("~/.claude/projects/-Users-yonghaekim/memory").expanduser(),
    Path("~/.claude/projects/-Users-yonghaekim-my-folder/memory").expanduser(),
]

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


def generate(force: bool = False, limit: int | None = None) -> dict:
    """모든 메모리 .md → alias_index.json 갱신.

    force=False 면 이미 index 에 있는 path 는 skip (incremental). True 면 전건 재생성.
    반환: stats.
    """
    existing: dict[str, dict] = {}
    if INDEX_PATH.exists() and not force:
        try:
            existing = json.loads(INDEX_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    targets: list[Path] = []
    for d in MEMORY_DIRS:
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            targets.append(md)
        # _procedural/ 하위도 포함
        proc = d / "_procedural"
        if proc.is_dir():
            for md in sorted(proc.glob("*.md")):
                targets.append(md)

    if limit is not None:
        targets = targets[:limit]

    stats = {"total": len(targets), "generated": 0, "skipped": 0, "failed": 0}
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
        aliases = _call_gemma(desc, body)
        if not aliases:
            stats["failed"] += 1
            _debug(f"no aliases: {name}")
            continue
        existing[path_key] = {
            "name": name,
            "aliases": aliases,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        stats["generated"] += 1
        # 매 10건마다 중간 저장 — Gemma 도중 실패해도 진행 보존
        if stats["generated"] % 10 == 0:
            _save(existing)
            print(f"  [{i+1}/{len(targets)}] {stats['generated']} ok ({(time.time()-t0):.0f}s)")
    _save(existing)
    stats["elapsed_s"] = round(time.time() - t0, 1)
    return stats


def _save(data: dict) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


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
    p.add_argument("--force", action="store_true", help="기존 alias_index 전건 재생성")
    p.add_argument("--limit", type=int, default=None, help="최대 N건만 처리 (디버그)")
    args = p.parse_args()
    s = generate(force=args.force, limit=args.limit)
    print(f"\nalias_index → {INDEX_PATH}")
    print(f"  total={s['total']} generated={s['generated']} skipped={s['skipped']} failed={s['failed']} elapsed={s['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
