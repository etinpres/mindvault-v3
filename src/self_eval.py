#!/usr/bin/env python3
"""MindVault v3 Sprint 15 — Self-evaluation Loop (측정 인프라).

핵심 metric 4가지를 metrics.jsonl + Claude session JSONL 로그로부터 batch 계산.
- hit rate: hook 회수에서 picked > 0 비율 (기존 metrics 직접 집계)
- internal effort: recall 후 다음 user turn 까지 assistant tool_use 평균 횟수
- false positive rate: recall 후 다음 user turn 에 negative cue (관계없, 엉뚱한, ...) 비율
- self-affirming flag: 자기충족 메모리 후보 (잘 작동·안정적·성공 키워드 N+) 목록

자동 게이트 조정은 의도적으로 미구현 — 잘못 학습된 loop 가 게이트를 망가뜨릴
위험이 더 큼 (V3-PLAN §7 위험 매트릭스). 본 sprint 는 metric 노출까지. 게이트
조정은 형이 분석 결과 보고 수동 결정.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DATA_DIR = Path("/Users/yonghaekim/.claude/mindvault-v3")
DEFAULT_METRICS = DATA_DIR / "metrics.jsonl"
DEFAULT_PROJECTS_ROOT = Path("/Users/yonghaekim/.claude/projects")
DEBUG_LOG = DATA_DIR / "debug.log"

# Sprint 15: hook 회수 직후 다음 사용자 turn 에서 자주 나타나는 negative cue 패턴.
# 형이 "이거 관계없는데" 식으로 받아치면 그 회수는 false positive 강력 신호.
# 단어 단위 안정 매칭 — 너무 짧은 부분 매칭(예: "없")으로 일반 부정문이 잡히지 않게
# context 단어 묶어서.
NEGATIVE_CUE_RE = re.compile(
    r"(관계\s?없|관련\s?없|상관\s?없|엉뚱한|"
    r"왜\s?(?:이게|그게|이거|그거).*(?:회수|나왔|떠올|보여)|"
    r"이거\s?아니(?:야|라)|그게\s?아니(?:야|라)|"
    r"원하는\s?게\s?아니|필요\s?없는데|쓸데\s?없|"
    r"잘못\s?(?:회수|골랐|불러))"
)

# self-affirming 키워드 — 본문에 2회 이상 등장하면 자기충족 flag.
# "MindVault v3 운영 중 (품질 양호)" 같은 echo chamber 식 메모리 잡기.
SELF_AFFIRMING_RE = re.compile(
    r"(잘\s?작동|안정적|완성|문제\s?없|정상\s?작동|성공적으로|"
    r"ship[-\s]?ready|production[-\s]?ready|품질\s?양호|운영\s?중)"
)
SELF_AFFIRMING_MIN_HITS = 2


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] self-eval: {msg}\n")
    except Exception:
        pass


def _parse_ts(ts: str) -> float | None:
    """ISO 형식 ts 를 unix timestamp (float seconds). 실패 시 None.

    metrics.jsonl 는 "2026-05-23T01:58:34" (Z 없음, local time 가정).
    Claude JSONL 은 "2026-05-22T17:05:26.819Z" (UTC).
    둘 다 처리 — local naive 는 fromisoformat 으로, Z 는 +00:00 으로 치환.
    """
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            # naive → local time 으로 간주
            dt = dt.astimezone()
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def load_recall_events(
    metrics_path: Path = DEFAULT_METRICS,
    since_unix: float | None = None,
    kinds: tuple[str, ...] = ("recall",),
) -> list[dict]:
    """metrics.jsonl 의 kind ∈ kinds 이벤트만 시간순. since_unix 이후만.

    Sprint 16 이후 hook 가 'recall_skip' 도 기록 (chat/meta 차단). 분류 분포 분석
    시 양쪽 모두 필요해 kinds tuple 로 확장.
    """
    out: list[dict] = []
    if not metrics_path.is_file():
        return out
    try:
        with metrics_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("kind") not in kinds:
                    continue
                ts_unix = _parse_ts(d.get("ts", ""))
                if ts_unix is None:
                    continue
                if since_unix is not None and ts_unix < since_unix:
                    continue
                d["_ts_unix"] = ts_unix
                out.append(d)
    except OSError as e:
        _debug(f"metrics read fail: {e}")
        return []
    out.sort(key=lambda d: d["_ts_unix"])
    return out


def _intent_stats_from_events(
    recall_events: list[dict], skip_events: list[dict]
) -> dict:
    """metrics.jsonl 의 intent 필드 기반 운영 분포.

    - recall_events: kind=recall (분류기 통과해 회수 시도)
    - skip_events: kind=recall_skip (chat/meta 강제 차단)

    Sprint 16 이전 recall 은 intent 필드 없음 → 'pre-sprint16' bucket.
    """
    by_intent: dict[str, dict] = {}
    for e in recall_events:
        intent = e.get("intent") or "pre-sprint16"
        b = by_intent.setdefault(
            intent, {"recall_attempts": 0, "picked": 0, "skipped": 0}
        )
        b["recall_attempts"] += 1
        if (e.get("picked") or 0) > 0:
            b["picked"] += 1
    for e in skip_events:
        intent = e.get("intent") or "unknown_skip"
        b = by_intent.setdefault(
            intent, {"recall_attempts": 0, "picked": 0, "skipped": 0}
        )
        b["skipped"] += 1
    # per-intent hit_rate (recall_attempts 기준)
    out = {}
    total_skip = sum(v["skipped"] for v in by_intent.values())
    total_attempt = sum(v["recall_attempts"] for v in by_intent.values())
    for intent, v in by_intent.items():
        attempts = v["recall_attempts"]
        hit_rate = (v["picked"] / attempts) if attempts else 0.0
        total = attempts + v["skipped"]
        out[intent] = {
            **v,
            "total": total,
            "hit_rate": hit_rate,
        }
    return {
        "by_intent": out,
        "total_attempts": total_attempt,
        "total_skipped": total_skip,
        "skip_ratio_of_all": (
            total_skip / (total_attempt + total_skip)
            if (total_attempt + total_skip) else 0.0
        ),
    }


def _iter_bash_commands(projects_root: Path, since_unix: float):
    """모든 jsonl assistant turn 의 Bash tool_use input.command 순회.

    yields (ts_unix, command_str). Bash 외 tool (Read, Edit, ...) 은 명령어 분석 대상 X.
    """
    for jp in iter_session_jsonl_paths(projects_root):
        try:
            with jp.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    ts = _parse_ts(d.get("timestamp", ""))
                    if ts is None or ts < since_unix:
                        continue
                    content = (d.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") != "tool_use" or b.get("name") != "Bash":
                            continue
                        inp = b.get("input") or {}
                        cmd = inp.get("command")
                        if isinstance(cmd, str) and cmd.strip():
                            yield ts, cmd.strip()
        except OSError:
            continue


# 명령어 첫 token 추출용. shell builtin (cd, echo, sleep) + 흔한 pipeline 잡음 제외
_BIN_TOKEN_RE = re.compile(r"^([A-Za-z_][\w.-]*)")
_SKIP_BINS = {
    # shell builtin / 사소한 잡음
    "cd", "pwd", "echo", "printf", "true", "false", "exit", "return",
    "sleep", "test", "if", "then", "fi", "for", "while", "do", "done",
    "alias", "export", "unset", "source", "read", "set",
    # 단순 viewer
    "cat", "head", "tail", "less", "more", "wc", "tr", "cut", "sort", "uniq",
    "grep", "rg", "find", "fd", "awk", "sed", "ls", "stat", "file",
    # path manipulation
    "basename", "dirname", "realpath", "mkdir", "touch", "rm", "cp", "mv", "ln",
    # 흔한 utility
    "date", "which", "type", "command", "env", "uname", "id",
}


def _extract_command_bin(cmd: str) -> str | None:
    """명령어 첫 token (실행 binary 이름). 의미 있는 명령어만 반환.

    파이프·redirection 등 노이즈 잘려 첫 단어만 본다. shell builtin 은 skip.
    `claude --bg`, `git worktree`, `npm install`, `python3 ...` 같은 진짜 명령어만.
    `PATH=/x foo`, `ENV=val cmd` 같은 env var assignment 도 skip.
    """
    stripped = cmd.lstrip()
    first_line = stripped.split("\n", 1)[0]
    first_line = re.sub(r"\$\([^)]*\)", "", first_line)
    # env var assignment 패턴 (`NAME=value` 가 첫 token) 검사
    first_token_str = first_line.split(None, 1)[0] if first_line.split() else ""
    if "=" in first_token_str:
        return None
    m = _BIN_TOKEN_RE.match(first_line)
    if not m:
        return None
    bin_name = m.group(1)
    if bin_name in _SKIP_BINS:
        return None
    return bin_name


def audit_procedural_coverage(
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    memory_dirs: list[Path] | None = None,
    hours_back: int = 720,  # 30일 기본 — 더 넓은 표본
    top_n: int = 20,
) -> dict:
    """Bash tool_use 명령어 분포 + procedural memory 보유 여부.

    coverage = (top_n 명령어 중 procedural slot 또는 본문에 명령어 포함된 메모리 보유) /
               top_n.

    procedural slot 후보 = path 에 `/_procedural/` 포함 OR frontmatter type=procedural.
    매칭: 명령어 이름이 memory name 또는 body 에 등장.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from memory_indexer import (  # noqa: WPS433
        DEFAULT_MEMORY_DIRS as _DEF_DIRS,
        _extra_memory_dirs as _xtra,
        _collect_md_files as _collect,
        parse_frontmatter as _parse_fm,
    )
    if memory_dirs is None:
        memory_dirs = _DEF_DIRS + _xtra()
    since = time.time() - hours_back * 3600

    from collections import Counter
    counter: Counter[str] = Counter()
    for _ts, cmd in _iter_bash_commands(projects_root, since):
        bin_name = _extract_command_bin(cmd)
        if bin_name:
            counter[bin_name] += 1

    top = counter.most_common(top_n)

    # procedural 메모리 후보 인벤토리
    proc_memos: list[dict] = []
    for p in _collect(memory_dirs):
        is_proc_slot = "/_procedural/" in str(p)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = _parse_fm(text)
        is_proc_type = (fm.get("type") or "").strip().lower() == "procedural"
        if is_proc_slot or is_proc_type:
            proc_memos.append({
                "path": str(p),
                "name": (fm.get("name") or p.stem),
                "body_lower": body.lower(),
            })

    # 매칭
    coverage_table: list[dict] = []
    covered = 0
    for bin_name, n in top:
        name_lower = bin_name.lower()
        memos_matched = [
            m["name"] for m in proc_memos
            if name_lower in m["name"].lower() or name_lower in m["body_lower"]
        ]
        is_covered = bool(memos_matched)
        coverage_table.append({
            "command": bin_name,
            "usage_count": n,
            "covered": is_covered,
            "matched_memories": memos_matched[:3],
        })
        if is_covered:
            covered += 1

    return {
        "hours_back": hours_back,
        "total_bash_commands_examined": sum(counter.values()),
        "unique_binaries": len(counter),
        "top_n": top_n,
        "procedural_memory_count": len(proc_memos),
        "coverage_ratio": (covered / top_n) if top_n else 0.0,
        "covered": covered,
        "table": coverage_table,
    }


def classify_user_turns(
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    hours_back: int = 168,
    sample_per_intent: int = 5,
) -> dict:
    """모든 jsonl 의 user turn 에 query_intent.classify 돌려 분포 + 표본.

    metrics.jsonl 은 Sprint 16 이후만 intent 기록. 그 이전 turn 까지 포함한 더 큰
    표본 (~수개월) 으로 classifier 의 운영 분포 측정. ground-truth 라벨링은 아니지만
    분포 sanity 와 의외 매칭 패턴 검출에 충분.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from query_intent import classify
    except ImportError as e:
        _debug(f"query_intent import fail: {e}")
        return {"error": "query_intent module missing"}
    since = time.time() - hours_back * 3600
    counter: dict[str, list] = {
        "chat": [], "meta": [], "code": [], "recall": [], "unknown": []
    }
    total_examined = 0
    for jp in iter_session_jsonl_paths(projects_root):
        for turn in load_turns(jp):
            if turn["role"] != "user":
                continue
            if turn["ts_unix"] < since:
                continue
            text = turn["text"]
            if len(text) < 4:  # hook MIN_PROMPT_LEN 와 정합
                continue
            r = classify(text)
            total_examined += 1
            counter.setdefault(r.intent, []).append({
                "ts_unix": turn["ts_unix"],
                "text": text[:100],
                "matched": list(r.matched)[:3],
            })
    result = {
        "hours_back": hours_back,
        "total_user_turns_examined": total_examined,
        "by_intent": {},
    }
    for intent, lst in counter.items():
        result["by_intent"][intent] = {
            "count": len(lst),
            "ratio": (len(lst) / total_examined) if total_examined else 0.0,
            "sample": lst[:sample_per_intent],
        }
    return result


def iter_session_jsonl_paths(
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
) -> Iterable[Path]:
    if not projects_root.is_dir():
        return
    for p in projects_root.glob("*/*.jsonl"):
        yield p


def _extract_text(content) -> str:
    """Claude JSONL message.content → text. tool_use 블록 등 메타는 빈 문자열."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") in (None, "text"):
            t = b.get("text") or ""
            parts.append(str(t))
    return "\n".join(parts)


def _is_system_reminder(text: str) -> bool:
    head = (text or "").lstrip()[:64]
    return head.startswith("<system-reminder>") or head.startswith("<command-")


# Sprint 15 #3: SessionStart / 세션 요약 hook 가 user role 로 jsonl 에 주입하는
# Gemma·Claude system prompt 패턴. 형 직접 발화가 아니라 hook artifact 이므로
# classifier 분포·false positive 측정 시 표본에서 제외해야 정확.
HOOK_INJECTED_PREFIXES = (
    "다음은 Claude Code 세션",
    "# 지난 세션 요약",
    "이 대화의 마지막 부분",
)


def _is_hook_injected(text: str) -> bool:
    head = (text or "").lstrip()[:64]
    return any(head.startswith(p) for p in HOOK_INJECTED_PREFIXES)


def load_turns(jsonl_path: Path) -> list[dict]:
    """단일 jsonl → turn 시퀀스 (timestamp 정렬). 각 turn: {ts_unix, role, text, tool_uses}.

    role:
    - 'user': 실제 사용자 메시지 (system-reminder 제외)
    - 'assistant': assistant 응답 (tool_uses 리스트도 추출)
    """
    turns: list[dict] = []
    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = d.get("message") or {}
                ts_unix = _parse_ts(d.get("timestamp", ""))
                if ts_unix is None:
                    continue
                content = msg.get("content")
                text = _extract_text(content).strip()
                tool_uses: list[str] = []
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tool_uses.append(b.get("name") or "unknown")
                if t == "user" and (
                    _is_system_reminder(text) or _is_hook_injected(text)
                ):
                    continue
                # Sprint 15 #1: tool_result block 은 message.type=user 로 jsonl 에
                # 들어오지만 _extract_text 가 무시해 빈 text 가 됨. 빈 user turn 은
                # 실제 발화 아니므로 measure_post_recall 의 next_user 후보에서 제외.
                if t == "user" and not text:
                    continue
                turns.append({
                    "ts_unix": ts_unix,
                    "role": t,
                    "text": text,
                    "tool_uses": tool_uses,
                })
    except OSError as e:
        _debug(f"jsonl read fail {jsonl_path}: {e}")
        return []
    return turns


SHORT_NEXT_USER_CHARS = 15
# Sprint 15 #1: 다음 user turn 길이 < 15자 면 implicit FP 약한 신호 ("ㄴㄴ", "아니",
# "그거 말고" 식 격렬 부정). negative cue regex 안 잡혀도 짧음 자체가 시그널.
SESSION_GAP_SEC = 1800
# 30분 이상 다음 user turn 없으면 session abandoned 로 간주 → implicit FP 약한 신호


def measure_post_recall(turns: list[dict], recall_ts: float) -> dict:
    """recall_ts 직후 ~ 다음 user turn 직전 사이 assistant tool_use 카운트 + 다음 user 텍스트.

    반환:
    - tool_use_count: int
    - tool_use_breakdown: {name: count}
    - next_user_text: str | None
    - next_user_ts: float | None
    - next_user_chars: int (next_user_text 없으면 -1)
    - abandoned: bool — 30분 안에 다음 user turn 없음
    """
    out = {
        "tool_use_count": 0,
        "tool_use_breakdown": {},
        "next_user_text": None,
        "next_user_ts": None,
        "next_user_chars": -1,
        "abandoned": False,
    }
    if not turns:
        out["abandoned"] = True
        return out
    started = False
    for t in turns:
        if not started and t["ts_unix"] >= recall_ts - 5:
            started = True
        if not started:
            continue
        if t["role"] == "user" and t["ts_unix"] > recall_ts + 1:
            out["next_user_text"] = t["text"]
            out["next_user_ts"] = t["ts_unix"]
            out["next_user_chars"] = len(t["text"])
            break
        if t["role"] == "assistant":
            for name in t["tool_uses"]:
                out["tool_use_count"] += 1
                out["tool_use_breakdown"][name] = (
                    out["tool_use_breakdown"].get(name, 0) + 1
                )
    # next_user 없음 + recall 후 SESSION_GAP_SEC 안에 어떤 user turn 도 없으면
    # abandoned 로 표시. window 가 ±30분으로 잘려 있어 next_user 가 None 인 경우
    # = 그 안에서 형이 더 안 응답 = 사실상 abandoned.
    if out["next_user_text"] is None:
        out["abandoned"] = True
    return out


def implicit_fp_signal(post: dict) -> str | None:
    """measure_post_recall 결과 → implicit FP 약한 신호 분류.

    - 'short_next_user': 다음 user turn 길이 < SHORT_NEXT_USER_CHARS
    - 'abandoned': 다음 user turn 없이 30분 경과
    - None: 신호 없음
    """
    if post.get("abandoned"):
        return "abandoned"
    chars = post.get("next_user_chars", -1)
    if 0 <= chars < SHORT_NEXT_USER_CHARS:
        return "short_next_user"
    return None


def has_negative_cue(text: str) -> bool:
    if not text:
        return False
    return bool(NEGATIVE_CUE_RE.search(text))


def is_self_affirming(text: str, min_hits: int = SELF_AFFIRMING_MIN_HITS) -> bool:
    if not text:
        return False
    hits = SELF_AFFIRMING_RE.findall(text)
    return len(hits) >= min_hits


def scan_self_affirming_memories(
    memory_dirs: list[Path] | None = None,
) -> list[dict]:
    """기존 메모리 중 self-affirming 키워드가 N+ 회 등장하는 후보 목록."""
    if memory_dirs is None:
        # late import — circular 회피
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from memory_indexer import (
                DEFAULT_MEMORY_DIRS,
                _extra_memory_dirs,
                _collect_md_files,
                parse_frontmatter,
            )
            memory_dirs = DEFAULT_MEMORY_DIRS + _extra_memory_dirs()
            files = _collect_md_files(memory_dirs)
        except Exception as e:
            _debug(f"scan import fail: {e}")
            return []
    else:
        sys.path.insert(0, str(Path(__file__).parent))
        from memory_indexer import _collect_md_files, parse_frontmatter
        files = _collect_md_files(memory_dirs)

    out: list[dict] = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        hits = SELF_AFFIRMING_RE.findall(body)
        if len(hits) >= SELF_AFFIRMING_MIN_HITS:
            out.append({
                "path": str(p),
                "name": fm.get("name") or p.stem,
                "hit_count": len(hits),
                "sample_terms": sorted({h for h in hits})[:5],
            })
    out.sort(key=lambda x: x["hit_count"], reverse=True)
    return out


def analyze_recent(
    metrics_path: Path = DEFAULT_METRICS,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    hours_back: int = 168,
    use_cache: bool = True,
) -> dict:
    """최근 N 시간 recall 이벤트 분석 → metric dict.

    반환:
    - total_recalls
    - recalls_with_pick (picked > 0)
    - hit_rate
    - avg_internal_effort
    - false_positive_count / rate (next_user_text 에 negative cue)
    - self_affirming_memories: list (scan_self_affirming_memories 결과)
    - per_event: list of {ts, picked, raw, internal_effort, false_positive}
    """
    since = time.time() - hours_back * 3600
    events = load_recall_events(metrics_path, since_unix=since)
    skip_events = load_recall_events(
        metrics_path, since_unix=since, kinds=("recall_skip",)
    )
    total = len(events)
    picked = sum(1 for e in events if (e.get("picked") or 0) > 0)
    intent_stats = _intent_stats_from_events(events, skip_events)

    # Sprint NEXT-7: use_cache=True 면 turns_cache 가 incremental sqlite 인덱스 경유
    # → ~50s → <5s. since 필터도 SQL WHERE 으로 위임. 첫 build 는 동일 비용이지만
    # 그 후 mtime 변경 jsonl 만 재 parse. 기본 동작은 직접 parsing (rollback 경로).
    all_turns: list[dict] = []
    if use_cache:
        try:
            from turns_cache import get_turns_since  # noqa: WPS433
            all_turns = get_turns_since(since, projects_root=projects_root)
        except Exception as e:
            _debug(f"turns_cache fail, fallback to direct parsing: {e}")
            all_turns = []
    if not all_turns:
        for jp in iter_session_jsonl_paths(projects_root):
            all_turns.extend(load_turns(jp))
        all_turns.sort(key=lambda t: t["ts_unix"])

    per_event: list[dict] = []
    effort_values: list[int] = []
    fp_strong = 0  # explicit negative cue
    fp_known_explicit = 0  # next_user_text 식별 가능 — explicit FP 분모
    fp_implicit_short = 0  # 짧은 다음 user turn
    fp_implicit_abandoned = 0  # 다음 user turn 없음
    for e in events:
        ts_unix = e["_ts_unix"]
        window = [
            t for t in all_turns if ts_unix - 1 <= t["ts_unix"] <= ts_unix + 1800
        ]
        post = measure_post_recall(window, ts_unix)
        effort = post["tool_use_count"]
        effort_values.append(effort)
        fp_explicit = False
        if post["next_user_text"]:
            fp_explicit = has_negative_cue(post["next_user_text"])
            fp_known_explicit += 1
            if fp_explicit:
                fp_strong += 1
        implicit = implicit_fp_signal(post)
        if implicit == "short_next_user":
            fp_implicit_short += 1
        elif implicit == "abandoned":
            fp_implicit_abandoned += 1
        per_event.append({
            "ts": e.get("ts"),
            "picked": e.get("picked"),
            "raw_top1_cosine": e.get("raw_top1_cosine"),
            "raw_min": e.get("raw_min"),
            "internal_effort": effort,
            "tool_breakdown": post["tool_use_breakdown"],
            "next_user_known": post["next_user_text"] is not None,
            "false_positive_explicit": fp_explicit,
            "false_positive_implicit": implicit,
        })

    fp_combined = (
        fp_strong + fp_implicit_short + fp_implicit_abandoned
    )
    fp_combined_rate = (fp_combined / total) if total else 0.0

    effort_stats = _effort_stats(effort_values)
    return {
        "since_unix": since,
        "hours_back": hours_back,
        "total_recalls": total,
        "recalls_with_pick": picked,
        "hit_rate": (picked / total) if total else 0.0,
        "avg_internal_effort": effort_stats["avg"],
        "internal_effort": effort_stats,
        "intent_stats": intent_stats,
        # explicit (negative cue) — Sprint 15 기존 metric
        "false_positive_known": fp_known_explicit,
        "false_positive_count": fp_strong,
        "false_positive_rate": (
            (fp_strong / fp_known_explicit) if fp_known_explicit else 0.0
        ),
        # implicit (Sprint 15 #1 보강)
        "false_positive_implicit": {
            "short_next_user": fp_implicit_short,
            "abandoned": fp_implicit_abandoned,
            "combined_strong_implicit": fp_combined,
            "combined_rate_over_total": fp_combined_rate,
        },
        "self_affirming_memories": scan_self_affirming_memories(),
        "per_event_count": len(per_event),
        "per_event_sample": per_event[:5],
    }


def _percentile(sorted_values: list[int], p: float) -> float:
    """단순 nearest-rank percentile. p ∈ [0, 100]. 빈 list 면 0."""
    if not sorted_values:
        return 0.0
    if p <= 0:
        return float(sorted_values[0])
    if p >= 100:
        return float(sorted_values[-1])
    # rank = ceil(p/100 * n) - 1 (0-indexed nearest-rank)
    import math
    rank = max(0, math.ceil(p / 100.0 * len(sorted_values)) - 1)
    return float(sorted_values[rank])


def _effort_stats(values: list[int]) -> dict:
    """internal effort 분포 — avg + bucket histogram + percentile + long-tail 비율.

    histogram bucket:
    - 0: recall 후 추가 tool 0회 (회수 충분 신호)
    - 1: tool 1회 (정상)
    - 2-4: 평균적 후속 작업
    - 5+: long-tail (회수 부족 가능성)
    """
    n = len(values)
    if n == 0:
        return {
            "n": 0, "avg": 0.0,
            "histogram": {"0": 0, "1": 0, "2-4": 0, "5+": 0},
            "p50": 0.0, "p90": 0.0, "p99": 0.0,
            "long_tail_ratio": 0.0, "max": 0,
        }
    avg = sum(values) / n
    hist = {"0": 0, "1": 0, "2-4": 0, "5+": 0}
    for v in values:
        if v == 0:
            hist["0"] += 1
        elif v == 1:
            hist["1"] += 1
        elif v <= 4:
            hist["2-4"] += 1
        else:
            hist["5+"] += 1
    sorted_vals = sorted(values)
    return {
        "n": n,
        "avg": avg,
        "histogram": hist,
        "p50": _percentile(sorted_vals, 50),
        "p90": _percentile(sorted_vals, 90),
        "p99": _percentile(sorted_vals, 99),
        "long_tail_ratio": hist["5+"] / n,
        "max": sorted_vals[-1],
    }


def format_report(summary: dict) -> str:
    eff = summary.get("internal_effort") or {}
    hist = eff.get("histogram", {})
    lines = [
        "# MindVault Self-eval Report",
        f"window: 최근 {summary['hours_back']}h, total_recalls={summary['total_recalls']}",
        f"hit rate: {summary['hit_rate']*100:.1f}% "
        f"({summary['recalls_with_pick']}/{summary['total_recalls']})",
        f"internal effort (tool_use after recall):",
        f"  avg={eff.get('avg', summary.get('avg_internal_effort', 0)):.2f}  "
        f"p50={eff.get('p50', 0):.0f}  p90={eff.get('p90', 0):.0f}  "
        f"p99={eff.get('p99', 0):.0f}  max={eff.get('max', 0)}",
        f"  histogram: 0={hist.get('0', 0)}  1={hist.get('1', 0)}  "
        f"2-4={hist.get('2-4', 0)}  5+={hist.get('5+', 0)}",
        f"  long-tail ratio (5+ tool_use): {eff.get('long_tail_ratio', 0)*100:.1f}%",
        f"false positive (explicit negative cue): {summary['false_positive_rate']*100:.1f}% "
        f"({summary['false_positive_count']}/{summary['false_positive_known']}, "
        f"표본=다음 user turn 식별 가능)",
    ]
    impl = summary.get("false_positive_implicit") or {}
    if impl:
        lines.append(
            f"false positive (implicit, 약한 신호): "
            f"short_next_user={impl.get('short_next_user', 0)}  "
            f"abandoned={impl.get('abandoned', 0)}  "
            f"combined_rate_over_total={impl.get('combined_rate_over_total', 0)*100:.1f}%"
        )
    intent = summary.get("intent_stats") or {}
    by_intent = intent.get("by_intent") or {}
    if by_intent:
        lines.append("")
        lines.append(
            f"intent 분포 (hook 분류기 운영 기록): "
            f"total={intent.get('total_attempts', 0)} + "
            f"skip={intent.get('total_skipped', 0)} | "
            f"skip ratio={intent.get('skip_ratio_of_all', 0)*100:.1f}%"
        )
        for intent_name, v in sorted(
            by_intent.items(), key=lambda kv: -kv[1].get("total", 0)
        ):
            lines.append(
                f"  {intent_name:14s} total={v.get('total', 0):4d}  "
                f"attempts={v.get('recall_attempts', 0):4d}  "
                f"picked={v.get('picked', 0):4d}  "
                f"skipped={v.get('skipped', 0):4d}  "
                f"hit_rate={v.get('hit_rate', 0)*100:5.1f}%"
            )
    lines.append("")
    lines.append(
        f"self-affirming memory 후보: {len(summary['self_affirming_memories'])}건"
    )
    for m in summary["self_affirming_memories"][:5]:
        lines.append(
            f"  - {m['name']} ({m['hit_count']} hits) — {m['sample_terms']}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=168)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument(
        "--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--classifier-audit",
        action="store_true",
        help="user turn 전체에 classify 돌려 분포 + 표본 출력 (운영 검증)",
    )
    parser.add_argument(
        "--procedural-audit",
        action="store_true",
        help="Bash tool_use 명령어 분포 + procedural memory coverage 측정",
    )
    parser.add_argument(
        "--use-cache",
        dest="use_cache",
        action="store_true",
        default=True,
        help="Sprint NEXT-7 turns_cache 경유 (mtime 기반 incremental). default ON",
    )
    parser.add_argument(
        "--no-cache",
        dest="use_cache",
        action="store_false",
        help="turns_cache 우회, 직접 jsonl parsing (rollback 경로)",
    )
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="turns_cache 전체 재빌드 (mtime 무시)",
    )
    args = parser.parse_args()
    if args.rebuild_cache:
        try:
            from turns_cache import refresh_cache  # noqa: WPS433
            stat = refresh_cache(projects_root=args.projects_root, full=True)
            _debug(f"cache rebuild: {stat}")
        except Exception as e:
            _debug(f"cache rebuild fail: {e}")
    try:
        if args.classifier_audit:
            out = classify_user_turns(
                projects_root=args.projects_root, hours_back=args.hours
            )
            json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
            return 0
        if args.procedural_audit:
            out = audit_procedural_coverage(
                projects_root=args.projects_root, hours_back=args.hours
            )
            json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
            return 0
        summary = analyze_recent(
            metrics_path=args.metrics,
            projects_root=args.projects_root,
            hours_back=args.hours,
            use_cache=args.use_cache,
        )
        if args.json:
            json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
        else:
            print(format_report(summary))
        return 0
    except Exception as e:
        _debug(f"main FATAL: {e}\n{traceback.format_exc()}")
        print(f"self-eval failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
