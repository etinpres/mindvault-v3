#!/usr/bin/env python3
"""Phase 1③ 신뢰성 검증 CLI — stale 재검증 scan / list / 레지스트리 self-check.

usage:
  python -m src.reverify_cli scan <memory_dir> [--json]
  python -m src.reverify_cli list <memory_dir>
  python -m src.reverify_cli verify-registry
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from reverify import (  # noqa: E402
    scan_memories,
    verify_registry,
    default_root,
    _collect_memory_files,
    _current_reverify_status,
    _current_reverify_note,
)


def _cmd_scan(args) -> int:
    stats = scan_memories(Path(args.memory_dir).expanduser())
    if args.json:
        json.dump(stats, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(
            f"scan: flagged={stats['flagged']} cleared={stats['cleared']} "
            f"processed={stats['processed']}/{stats['total']}"
        )
    return 0


def _cmd_list(args) -> int:
    n = 0
    for p in _collect_memory_files(Path(args.memory_dir).expanduser()):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _current_reverify_status(text) == "stale":
            n += 1
            print(f"{p.name}: {_current_reverify_note(text)}")
    if n == 0:
        print("stale 메모리 없음")
    return 0


def _cmd_verify_registry(args) -> int:
    failed = verify_registry(default_root())
    if not failed:
        print("registry verify-registry: OK (모든 fact 라이브 통과)")
        return 0
    print("registry STALE — verifier fail:")
    for f in failed:
        print(f"  - {f['key']}: {f['description']}")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reverify_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="메모리 stale 재검증 + frontmatter flag 갱신")
    p_scan.add_argument("memory_dir")
    p_scan.add_argument("--json", action="store_true")
    p_scan.set_defaults(func=_cmd_scan)

    p_list = sub.add_parser("list", help="현재 stale flag 된 메모리 나열")
    p_list.add_argument("memory_dir")
    p_list.set_defaults(func=_cmd_list)

    p_vr = sub.add_parser("verify-registry", help="레지스트리 self-check (current_value 라이브?)")
    p_vr.set_defaults(func=_cmd_verify_registry)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
