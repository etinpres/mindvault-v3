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

DATA_DIR = Path("/Users/yonghaekim/.claude/mindvault-v2")
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
# "MindVault v2 운영 중 (품질 양호)" 같은 echo chamber 식 메모리 잡기.
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
) -> list[dict]:
    """metrics.jsonl 의 kind=recall 이벤트만 시간순. since_unix 이후만."""
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
                if d.get("kind") != "recall":
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
                if t == "user" and _is_system_reminder(text):
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


def measure_post_recall(turns: list[dict], recall_ts: float) -> dict:
    """recall_ts 직후 ~ 다음 user turn 직전 사이 assistant tool_use 카운트 + 다음 user 텍스트.

    반환:
    - tool_use_count: int (recall 후 ~ 다음 user 전 assistant turn 들의 tool_use 합)
    - tool_use_breakdown: {name: count}
    - next_user_text: str | None
    - next_user_ts: float | None
    """
    out = {
        "tool_use_count": 0,
        "tool_use_breakdown": {},
        "next_user_text": None,
        "next_user_ts": None,
    }
    if not turns:
        return out
    # recall_ts 직후의 turn 찾기. tolerance 5s (timezone naive 처리·hook latency).
    started = False
    for t in turns:
        if not started and t["ts_unix"] >= recall_ts - 5:
            started = True
        if not started:
            continue
        if t["role"] == "user" and t["ts_unix"] > recall_ts + 1:
            out["next_user_text"] = t["text"]
            out["next_user_ts"] = t["ts_unix"]
            break
        if t["role"] == "assistant":
            for name in t["tool_uses"]:
                out["tool_use_count"] += 1
                out["tool_use_breakdown"][name] = (
                    out["tool_use_breakdown"].get(name, 0) + 1
                )
    return out


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
    total = len(events)
    picked = sum(1 for e in events if (e.get("picked") or 0) > 0)

    # 모든 session jsonl turn 로드 후 시간 정렬. 메모리 사용량 주의 — 형 환경
    # ~700 jsonl x 평균 200 turn = 14만 turn. dict 단순 list 면 ~50MB 정도. OK.
    all_turns: list[dict] = []
    for jp in iter_session_jsonl_paths(projects_root):
        all_turns.extend(load_turns(jp))
    all_turns.sort(key=lambda t: t["ts_unix"])

    per_event: list[dict] = []
    effort_sum = 0
    effort_n = 0
    fp_count = 0
    fp_known = 0
    for e in events:
        ts_unix = e["_ts_unix"]
        # turns 중 ts ± 30분 윈도우만 보고 후속 분석 (불필요한 전체 순회 회피).
        window = [
            t for t in all_turns if ts_unix - 1 <= t["ts_unix"] <= ts_unix + 1800
        ]
        post = measure_post_recall(window, ts_unix)
        effort = post["tool_use_count"]
        effort_sum += effort
        effort_n += 1
        fp = False
        if post["next_user_text"]:
            fp = has_negative_cue(post["next_user_text"])
            fp_known += 1
            if fp:
                fp_count += 1
        per_event.append({
            "ts": e.get("ts"),
            "picked": e.get("picked"),
            "raw_top1_cosine": e.get("raw_top1_cosine"),
            "raw_min": e.get("raw_min"),
            "internal_effort": effort,
            "tool_breakdown": post["tool_use_breakdown"],
            "next_user_known": post["next_user_text"] is not None,
            "false_positive": fp,
        })

    return {
        "since_unix": since,
        "hours_back": hours_back,
        "total_recalls": total,
        "recalls_with_pick": picked,
        "hit_rate": (picked / total) if total else 0.0,
        "avg_internal_effort": (effort_sum / effort_n) if effort_n else 0.0,
        "false_positive_known": fp_known,
        "false_positive_count": fp_count,
        "false_positive_rate": (fp_count / fp_known) if fp_known else 0.0,
        "self_affirming_memories": scan_self_affirming_memories(),
        "per_event_count": len(per_event),
        "per_event_sample": per_event[:5],  # 큰 출력 회피
    }


def format_report(summary: dict) -> str:
    lines = [
        "# MindVault Self-eval Report",
        f"window: 최근 {summary['hours_back']}h, total_recalls={summary['total_recalls']}",
        f"hit rate: {summary['hit_rate']*100:.1f}% "
        f"({summary['recalls_with_pick']}/{summary['total_recalls']})",
        f"avg internal effort (tool_use after recall): "
        f"{summary['avg_internal_effort']:.2f}",
        f"false positive rate: {summary['false_positive_rate']*100:.1f}% "
        f"({summary['false_positive_count']}/{summary['false_positive_known']}, "
        f"표본=다음 user turn 식별 가능한 recall)",
        "",
        f"self-affirming memory 후보: {len(summary['self_affirming_memories'])}건",
    ]
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
    args = parser.parse_args()
    try:
        summary = analyze_recent(
            metrics_path=args.metrics,
            projects_root=args.projects_root,
            hours_back=args.hours,
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
