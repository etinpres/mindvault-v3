"""Review CLI for the contradiction queue (~/.claude/mindvault-v3/contradictions.jsonl).

T6 covers: list / show / resolve dry-run.
T7 adds:  resolve --apply (dismiss / supersede / update mutations).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def _queue_path() -> Path:
    """contradictions.jsonl location. MV3_RUNTIME_DIR env override (matches T4)."""
    env = os.environ.get("MV3_RUNTIME_DIR")
    base = Path(env) if env else Path.home() / ".claude" / "mindvault-v3"
    return base / "contradictions.jsonl"


def load_all() -> list[dict]:
    """Read all jsonl rows (including resolved), skipping malformed lines.

    Public — T7's atomic rewrite reuses this to preserve schema lock-step with T6.
    """
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
    return [d for d in load_all() if not d.get("resolved")]


def cmd_list(args) -> int:
    items = _load_unresolved()
    if not items:
        print("미해결 contradiction 없음.")
        return 0
    for i, d in enumerate(items, 1):
        kind = d.get("kind", "?")
        new_slug = d.get("new_slug", "?")
        target_name = d.get("target_name", "?")
        confidence = d.get("confidence", 0.0)
        reason = d.get("reason", "")
        try:
            conf_str = f"{float(confidence):.2f}"
        except (TypeError, ValueError):
            conf_str = "?"
        print(
            f"[{i}] {kind:18s} | new={new_slug:25s} "
            f"vs old={target_name:25s} | conf={conf_str}"
        )
        print(f"    {reason}")
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
    print(f"kind:       {d.get('kind', '?')}")
    print(f"new:        {d.get('new_slug', '?')}")
    print(f"  path:     {d.get('new_path', '?')}")
    print(f"old:        {d.get('target_name', '?')}")
    print(f"  path:     {d.get('target_path', '?')}")
    print(f"confidence: {d.get('confidence', '?')}")
    print(f"reason:     {d.get('reason', '')}")
    print()
    print("--- new excerpt ---")
    print(d.get("new_excerpt", ""))
    print()
    print("--- old excerpt ---")
    print(d.get("old_excerpt", ""))
    return 0


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Returns ('frontmatter content', 'body'). No frontmatter → ('', text)."""
    m = re.match(r"^---\n(.*?)\n---\n+", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def _extract_yaml_name(p: Path) -> str | None:
    """Read 'name:' from frontmatter."""
    text = _read_text(p)
    if not text:
        return None
    fm, _ = _split_frontmatter(text)
    m = re.search(r"^name:\s*(\S+)\s*$", fm, re.MULTILINE)
    return m.group(1) if m else None


def _patch_frontmatter_list(p: Path, key: str, value: str) -> bool:
    """Append value to frontmatter '{key}: [a, b]' list, idempotent.

    Refuses to mutate block-style YAML lists (e.g. `key:\\n  - a\\n  - b`) —
    naive append would create a duplicate key. User must convert to flow-style
    ([a, b]) first.
    """
    text = _read_text(p)
    if text is None:
        return False
    fm, body = _split_frontmatter(text)
    if not fm:
        return False

    # Refuse to mutate block-style YAML lists — would silently create duplicate keys.
    block_re = re.compile(
        rf"^{re.escape(key)}:\s*\n(\s+-\s+\S+\s*\n)+",
        re.MULTILINE,
    )
    if block_re.search(fm):
        try:
            import os as _os
            log_path = Path(
                _os.environ.get("MV3_RUNTIME_DIR")
                or (Path.home() / ".claude" / "mindvault-v3")
            ) / "debug.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            import time
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a", encoding="utf-8") as logf:
                logf.write(
                    f"[{ts}] contradiction-cli: refuse to mutate block-style "
                    f"YAML list {key!r} in {p}\n"
                )
        except OSError:
            pass
        return False

    line_re = re.compile(rf"^{re.escape(key)}:\s*\[(.*?)\]\s*$", re.MULTILINE)
    existing = line_re.search(fm)
    if existing:
        items = [s.strip() for s in existing.group(1).split(",") if s.strip()]
        if value in items:
            return True  # idempotent
        items.append(value)
        new_line = f"{key}: [{', '.join(items)}]"
        fm = line_re.sub(new_line, fm)
    else:
        fm = fm.rstrip() + f"\n{key}: [{value}]"

    # Atomic write: tmp + os.replace
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(f"---\n{fm}\n---\n\n{body.lstrip()}", encoding="utf-8")
    os.replace(tmp, p)
    return True


def _apply_supersede(new_path: Path, old_path: Path) -> bool:
    new_name = _extract_yaml_name(new_path)
    old_name = _extract_yaml_name(old_path)
    if not new_name or not old_name:
        return False
    ok1 = _patch_frontmatter_list(new_path, "supersedes", old_name)
    ok2 = _patch_frontmatter_list(old_path, "deprecated_by", new_name)
    return ok1 and ok2


def _apply_update(new_path: Path, old_path: Path) -> bool:
    """OLD body ← NEW body, frontmatter from OLD preserved, NEW deleted."""
    new_text = _read_text(new_path)
    old_text = _read_text(old_path)
    if new_text is None or old_text is None:
        return False
    old_fm, _ = _split_frontmatter(old_text)
    _, new_body = _split_frontmatter(new_text)
    if not old_fm:
        return False
    # Atomic write for OLD
    tmp = old_path.with_suffix(old_path.suffix + ".tmp")
    tmp.write_text(f"---\n{old_fm}\n---\n\n{new_body.lstrip()}", encoding="utf-8")
    os.replace(tmp, old_path)
    # Delete NEW (best-effort; missing_ok)
    try:
        new_path.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _mark_resolved(target_item: dict, new_status: str) -> bool:
    """Rewrite contradictions.jsonl with the target row's resolved field updated.

    Atomic: tmp + os.replace. Matches first unresolved row whose
    (new_slug, target_name) tuple matches target_item.

    Returns True if a row was marked, False otherwise.
    """
    import fcntl

    p = _queue_path()
    all_items = load_all()
    matched = False
    for d in all_items:
        if (
            d.get("new_slug") == target_item.get("new_slug")
            and d.get("target_name") == target_item.get("target_name")
            and not d.get("resolved")
        ):
            d["resolved"] = new_status
            matched = True
            break
    if not matched:
        return False

    tmp = p.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # best-effort lock
            f.write(
                "\n".join(json.dumps(d, ensure_ascii=False) for d in all_items)
                + "\n"
            )
        os.replace(tmp, p)
    except OSError:
        return False
    return True


def cmd_resolve(args) -> int:
    items = _load_unresolved()
    idx = args.idx - 1
    if not (0 <= idx < len(items)):
        print(
            f"인덱스 {args.idx} 범위 밖 (1..{len(items)})",
            file=sys.stderr,
        )
        return 1
    d = items[idx]
    new_slug = d.get("new_slug", "?")
    target_name = d.get("target_name", "?")

    if not args.apply:
        print(f"[{args.action}] {new_slug} vs {target_name}")
        print("(dry-run — --apply 추가 시 실제 적용)")
        return 0

    new_path = Path(d.get("new_path", "")) if d.get("new_path") else None
    old_path = Path(d.get("target_path", "")) if d.get("target_path") else None

    if args.action == "dismiss":
        if not _mark_resolved(d, "dismissed"):
            print("jsonl mark 실패", file=sys.stderr)
            return 2
        print(f"dismissed: {new_slug} vs {target_name}")
        return 0

    if args.action == "supersede":
        if not new_path or not new_path.exists():
            print(f"new_path 없음: {new_path}", file=sys.stderr)
            return 2
        if not old_path or not old_path.exists():
            print(f"old_path 없음: {old_path}", file=sys.stderr)
            return 2
        if not _apply_supersede(new_path, old_path):
            print("supersede frontmatter mutate 실패 (name 추출 실패?)", file=sys.stderr)
            return 2
        if not _mark_resolved(d, "superseded"):
            print(
                f"WARN: frontmatter patched but jsonl mark failed for {new_slug}. "
                "Re-run dismiss to clean up the queue.",
                file=sys.stderr,
            )
            return 2
        print(f"superseded: {new_slug} marks {target_name} as deprecated_by")
        return 0

    if args.action == "update":
        if not new_path or not new_path.exists():
            print(f"new_path 없음: {new_path}", file=sys.stderr)
            return 2
        if not old_path or not old_path.exists():
            print(f"old_path 없음: {old_path}", file=sys.stderr)
            return 2
        if not _apply_update(new_path, old_path):
            print("update 실패 (old frontmatter 없음?)", file=sys.stderr)
            return 2
        if not _mark_resolved(d, "updated"):
            print(
                f"WARN: update applied but jsonl mark failed for {new_slug}. "
                "Re-run dismiss to clean up the queue "
                "(NEW file already deleted, OLD already updated).",
                file=sys.stderr,
            )
            return 2
        print(f"updated: {target_name} body merged with {new_slug}, new deleted")
        return 0

    return 1


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
