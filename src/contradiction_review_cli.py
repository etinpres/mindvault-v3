"""Review CLI for the contradiction queue (~/.claude/mindvault-v3/contradictions.jsonl).

T6 covers: list / show / resolve dry-run.
T7 adds:  resolve --apply (dismiss / supersede / update mutations).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _queue_path() -> Path:
    """contradictions.jsonl location. MV3_RUNTIME_DIR env override (matches T4)."""
    env = os.environ.get("MV3_RUNTIME_DIR")
    base = Path(env) if env else Path.home() / ".claude" / "mindvault-v3"
    return base / "contradictions.jsonl"


def _load_all() -> list[dict]:
    """Read all jsonl rows, skipping malformed lines silently."""
    p = _queue_path()
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_unresolved() -> list[dict]:
    return [d for d in _load_all() if not d.get("resolved")]


def cmd_list(args) -> int:
    items = _load_unresolved()
    if not items:
        print("미해결 contradiction 없음.")
        return 0
    for i, d in enumerate(items, 1):
        print(
            f"[{i}] {d['kind']:18s} | new={d['new_slug']:25s} "
            f"vs old={d['target_name']:25s} | conf={d['confidence']:.2f}"
        )
        print(f"    {d['reason']}")
    return 0


def cmd_show(args) -> int:
    items = _load_unresolved()
    idx = args.idx - 1
    if not (0 <= idx < len(items)):
        print(
            f"인덱스 {args.idx} 범위 밖 (1..{len(items)})",
            file=sys.stderr,
        )
        return 1
    d = items[idx]
    print(f"=== Contradiction [{args.idx}] ===")
    print(f"kind:       {d['kind']}")
    print(f"new:        {d['new_slug']}")
    print(f"  path:     {d.get('new_path', '?')}")
    print(f"old:        {d['target_name']}")
    print(f"  path:     {d.get('target_path', '?')}")
    print(f"confidence: {d['confidence']}")
    print(f"reason:     {d['reason']}")
    print()
    print("--- new excerpt ---")
    print(d.get("new_excerpt", ""))
    print()
    print("--- old excerpt ---")
    print(d.get("old_excerpt", ""))
    return 0


def cmd_resolve(args) -> int:
    """T6: dry-run only. T7 will add --apply mutation."""
    items = _load_unresolved()
    idx = args.idx - 1
    if not (0 <= idx < len(items)):
        print(
            f"인덱스 {args.idx} 범위 밖 (1..{len(items)})",
            file=sys.stderr,
        )
        return 1
    d = items[idx]
    print(f"[{args.action}] {d['new_slug']} vs {d['target_name']}")
    if not args.apply:
        print("(dry-run — --apply 추가 시 실제 적용. T7 에서 활성화)")
    else:
        # T7 가 채울 자리. 지금은 dry-run 메시지만.
        print("(--apply는 T7 에서 활성화됩니다)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="contradiction_review_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="미해결 contradiction 항목 표시")

    sp_show = sub.add_parser("show", help="1건 디테일")
    sp_show.add_argument("idx", type=int)

    sp_res = sub.add_parser("resolve", help="결정 적용 (T6 dry-run / T7 apply)")
    sp_res.add_argument("idx", type=int)
    sp_res.add_argument(
        "--action",
        choices=["update", "supersede", "dismiss"],
        required=True,
    )
    sp_res.add_argument(
        "--apply",
        action="store_true",
        help="없으면 dry-run (mutate 없음). T7 에서 mutate 활성화.",
    )

    args = p.parse_args(argv)

    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "show":
        return cmd_show(args)
    if args.cmd == "resolve":
        return cmd_resolve(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
