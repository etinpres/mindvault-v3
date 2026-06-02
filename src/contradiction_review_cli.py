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
    """Returns ('frontmatter content', 'body'). No frontmatter → ('', text).

    Defect CRLF: accept CRLF line endings (\\r\\n) so manually-edited
    (Windows/Obsidian) memories aren't silently treated as frontmatter-less,
    which would skip the supersede mutation without any error.
    """
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n+", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def _extract_yaml_name(p: Path) -> str | None:
    """Read 'name:' from frontmatter (full human title, may contain spaces).

    Fix I-name: the previous ``\\S+`` + ``$`` anchor matched only single-token
    names, so the COMMON case written by session_memory_end.write_staged
    (``name: {item['title']}`` — a human title WITH SPACES) returned None and
    silently broke supersede. We now capture the whole line and strip
    surrounding quotes. Note: callers that embed an identifier into an inline
    frontmatter list must NOT use this value directly — a spaced/comma'd title
    would corrupt ``key: [a, b]``. Use ``_supersede_id`` (file stem) instead.
    """
    text = _read_text(p)
    if not text:
        return None
    fm, _ = _split_frontmatter(text)
    m = re.search(r"^name:\s*(.+?)\s*$", fm, re.MULTILINE)
    if not m:
        return None
    name = m.group(1).strip()
    # strip surrounding quotes (single or double)
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ("'", '"'):
        name = name[1:-1]
    return name or None


def _supersede_id(p: Path) -> str:
    """Identifier written into ``supersedes:`` / ``deprecated_by:`` inline lists.

    Decision (Fix I-name): use the FILE STEM (kebab slug, no spaces/commas),
    NOT the human ``name:`` title. Rationale:
      1. The contradiction queue already keys old memories by ``path.stem``
         (contradiction_detector.py:125 ``target_name=path.stem``), so the stem
         is the canonical cross-reference identifier in this system.
      2. The lists are flow-style inline lists (``key: [a, b]``); a multi-word
         or comma-bearing human title would corrupt the list syntax.
    The stem is guaranteed space/comma-free, so it round-trips safely.
    """
    return p.stem


def _BLOCK_LIST_RE(key: str) -> re.Pattern:
    """Detect a block-style YAML list (``key:\\n  - a\\n  - b``).

    Defect block-guard-edge: the previous regex required a trailing newline after
    the final ``- item`` (``\\s*\\n``), so a block list that is the LAST
    frontmatter key with no trailing newline (e.g. ``supersedes:\\n  - a`` at
    end-of-string) slipped past the guard — _patch_frontmatter_list would then
    append a duplicate inline ``supersedes: [...]`` key. We tolerate end-of-line
    OR end-of-string after each item (``(\\s*\\n|\\s*$)``) under re.MULTILINE.
    CRLF tolerated via the surrounding _split_frontmatter (\\r stripped there),
    but we also allow \\r before the newline defensively.
    """
    # bug-audit 2026-06-01 (blocklist-space-item-leak): \S+ 는 공백 포함 항목
    # (- some old memory)을 놓쳐 블록리스트 미탐지 → mutation 거부 가드를 뚫고
    # 중복 inline 키 추가로 YAML 손상. \S[^\r\n]* 로 항목 나머지(공백 포함)까지 매칭.
    return re.compile(
        rf"^{re.escape(key)}:[ \t]*\r?\n(\s+-\s+\S[^\r\n]*(\s*\r?\n|\s*$))+",
        re.MULTILINE,
    )


def _SCALAR_VALUE_RE(key: str) -> re.Pattern:
    """Detect a scalar (non-list) value: ``key: somevalue`` whose value is not a
    flow list ``[...]``.

    bug-audit 2026-05-29 (contradiction-scalar-dup-3): such a key matches neither
    the flow-list regex nor _BLOCK_LIST_RE, so _patch_frontmatter_list would fall
    to the else-branch and append a duplicate ``key: [..]`` line — a last-key-wins
    YAML corruption that drops the original scalar value. Treated like block-style:
    refuse to mutate (user must convert to flow list first). The negative lookahead
    ``(?!\\[)`` lets genuine flow lists (``key: [a, b]``) through.
    """
    return re.compile(rf"^{re.escape(key)}:[ \t]+(?!\[)\S", re.MULTILINE)


def _log_refuse_mutate(p: Path, key: str, style: str) -> None:
    """Telemetry: refused to mutate a non-flow-list frontmatter key (block/scalar)."""
    try:
        import os as _os
        import time as _time
        log_path = Path(
            _os.environ.get("MV3_RUNTIME_DIR")
            or (Path.home() / ".claude" / "mindvault-v3")
        ) / "debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(
                f"[{ts}] contradiction-cli: refuse to mutate {style} "
                f"YAML value {key!r} in {p}\n"
            )
    except OSError:
        pass


def _can_patch_frontmatter_list(p: Path, key: str) -> bool:
    """Dry validation: would ``_patch_frontmatter_list(p, key, ...)`` succeed?

    Mirrors the refusal conditions (unreadable, no frontmatter, block-style
    list) WITHOUT mutating. Used by _apply_supersede to validate BOTH files
    before mutating EITHER (Fix I-supersede-rollback).
    """
    text = _read_text(p)
    if text is None:
        return False
    fm, _ = _split_frontmatter(text)
    if not fm:
        return False
    if _BLOCK_LIST_RE(key).search(fm) or _SCALAR_VALUE_RE(key).search(fm):
        return False
    # bug-audit 2026-06-02 (#20): 키가 이미 존재하지만 정상 flow-list([a, b])로
    # 인식되지 않으면(예: 미닫힌 'key: [a, b') mutate 거부 — 그대로 두면 else 분기가
    # 중복 키를 append 해 last-key-wins YAML 손상. block/scalar 가드의 빈틈을 닫는다.
    _flow_re = re.compile(rf"^{re.escape(key)}:\s*\[(.*?)\]\s*$", re.MULTILINE)
    if re.search(rf"^{re.escape(key)}:", fm, re.MULTILINE) and not _flow_re.search(fm):
        return False
    return True


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

    # Refuse to mutate block-style OR scalar YAML values — naive append would
    # silently create a duplicate key (last-key-wins corruption). flow-list only.
    if _BLOCK_LIST_RE(key).search(fm):
        _log_refuse_mutate(p, key, "block-style")
        return False
    if _SCALAR_VALUE_RE(key).search(fm):
        _log_refuse_mutate(p, key, "scalar")
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
        # bug-audit 2026-06-02 (#20): 키가 이미 존재하나 정상 flow-list 가 아니면
        # (미닫힌 'key: [a, b' 등) 중복 키 append 대신 거부 — block/scalar 가드와
        # 동일 정책. 이미 깨진 YAML 을 더 손상시키지 않는다.
        if re.search(rf"^{re.escape(key)}:", fm, re.MULTILINE):
            _log_refuse_mutate(p, key, "malformed-non-flow-list")
            return False
        fm = fm.rstrip() + f"\n{key}: [{value}]"

    # Atomic write: tmp + os.replace (pid-suffixed tmp avoids concurrent races).
    tmp = p.with_suffix(f"{p.suffix}.{os.getpid()}.tmp")
    tmp.write_text(f"---\n{fm}\n---\n\n{body.lstrip()}", encoding="utf-8")
    os.replace(tmp, p)
    return True


def _apply_supersede(new_path: Path, old_path: Path) -> bool:
    """NEW supersedes OLD: NEW gets ``supersedes: [old]``, OLD gets
    ``deprecated_by: [new]``.

    Fix I-name: identifiers are file STEMS (slug, space-free), not the human
    ``name:`` titles — see ``_supersede_id``.

    Fix I-supersede-rollback: validate BOTH files are patchable (readable,
    have frontmatter, not block-style) BEFORE mutating EITHER. Previously NEW
    was patched first; if the OLD patch then failed (block-style refusal,
    OSError) NEW already claimed to supersede OLD while OLD lacked
    ``deprecated_by`` — an inconsistent half-state reported as failure. We
    require both readable names + both flow-style patchable up front, then
    patch OLD first (the file historically more likely to carry a block-style
    list and thus fail) and only patch NEW if OLD succeeded. This keeps the
    half-state window to a single unavoidable point (OLD ok / NEW write
    crash), which is the least harmful ordering: OLD-deprecated-without-NEW-
    supersedes is self-describing, whereas the reverse orphans a dangling
    supersede claim.
    """
    # Both files must expose a readable name (sanity: confirms valid memories).
    if not _extract_yaml_name(new_path) or not _extract_yaml_name(old_path):
        return False
    # Dry validation of BOTH patches before mutating either.
    if not _can_patch_frontmatter_list(new_path, "supersedes"):
        return False
    if not _can_patch_frontmatter_list(old_path, "deprecated_by"):
        return False

    new_id = _supersede_id(new_path)
    old_id = _supersede_id(old_path)
    # Patch OLD first (most likely to fail); abort before touching NEW if it does.
    if not _patch_frontmatter_list(old_path, "deprecated_by", new_id):
        return False
    if not _patch_frontmatter_list(new_path, "supersedes", old_id):
        return False
    return True


def _apply_update(new_path: Path, old_path: Path) -> bool:
    """OLD body ← NEW body, frontmatter from OLD preserved, NEW deleted.

    Fix I-update-unlink: previously the NEW unlink was best-effort and the
    function returned True even if unlink raised — leaving an orphaned NEW
    staged file while the queue was marked resolved. The merge itself
    (OLD body = NEW body) HAS succeeded at that point, so the data is safe;
    however an orphaned NEW file is a real defect: it will re-trigger
    detection / clutter _staged/ and the contradiction looks resolved but
    isn't fully cleaned up. Decision: treat unlink failure as a FAILURE
    (return False) so _mark_resolved is NOT called and the user sees the
    error and can retry / remove the orphan manually. ``missing_ok=True``
    means an already-absent NEW is fine (idempotent re-run); only a real
    OSError (e.g. permission) propagates as False.
    """
    new_text = _read_text(new_path)
    old_text = _read_text(old_path)
    if new_text is None or old_text is None:
        return False
    old_fm, _ = _split_frontmatter(old_text)
    _, new_body = _split_frontmatter(new_text)
    if not old_fm:
        return False
    # Atomic write for OLD (pid-suffixed tmp avoids concurrent-resolve races).
    tmp = old_path.with_suffix(f"{old_path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(f"---\n{old_fm}\n---\n\n{new_body.lstrip()}", encoding="utf-8")
    os.replace(tmp, old_path)
    # Delete NEW. missing_ok tolerates already-gone; any other OSError is a
    # failure (orphan left behind) — surface it so the queue stays unresolved.
    try:
        new_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _row_matches_target(row: dict, target_item: dict) -> bool:
    """Precise unresolved-row match for _mark_resolved (Fix I-mark-resolved-dup).

    The previous match used only the (new_slug, target_name) 2-tuple. Two queue
    rows can legitimately share that tuple (the same pair flagged by both the
    SessionEnd writer and the backfill pass, with different kind/ts/reason), so
    resolving the row at index 2 could mark index 1's row instead.

    We disambiguate by requiring EVERY scalar field present in target_item to
    match on the candidate row (excluding ``resolved`` itself — the target is
    the unresolved instance we picked from _load_unresolved). This is a
    superset of the old tuple and includes ts/kind/reason/confidence, which
    distinguishes duplicates. Non-scalar fields are compared by equality.
    """
    if row.get("resolved"):
        return False
    for k, v in target_item.items():
        if k == "resolved":
            continue
        if row.get(k) != v:
            return False
    return True


def _mark_resolved(target_item: dict, new_status: str) -> bool:
    """Rewrite contradictions.jsonl with the target row's resolved field updated.

    Atomic: pid-suffixed tmp + os.replace.

    Fix I-jsonl-loss: operate on RAW file lines, NOT load_all() output.
    load_all() silently drops malformed lines, and the previous implementation
    rewrote the queue from that filtered list — so resolving any row
    PERMANENTLY deleted every unparseable line (a single hand-edit typo →
    silent data loss). We now read the raw lines, parse each individually,
    mutate only the matched target line, and write ALL lines back including
    unparseable ones verbatim. Invariant: resolving one row never drops
    another row, parseable or not.

    Fix I-mark-resolved-dup: match via _row_matches_target (full-field
    composite) instead of the (new_slug, target_name) 2-tuple.

    Returns True if a row was marked, False otherwise.
    """
    import fcntl

    p = _queue_path()
    if not p.exists():
        return False

    # bug-audit 2026-05-29 (contradiction-jsonl-race-1): read-modify-rewrite 를
    # 공용 lock 파일 하에서 직렬화. 이전엔 tmp FD 에 flock 해(라이브 파일과 다른
    # inode라 무의미) read 와 os.replace 사이에 concurrent SessionEnd 의
    # append_to_review_queue 가 추가한 row 가 replace 로 덮여 유실됐다. append 측도
    # 같은 lock 파일(<queue>.lock)을 LOCK_EX 로 잡으므로 두 연산이 직렬화된다.
    lock_path = p.with_name(p.name + ".lock")
    try:
        lock_fh = open(lock_path, "w")
    except OSError:
        lock_fh = None
    try:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # best-effort lock

        raw_lines = p.read_text(encoding="utf-8").splitlines()
        out_lines: list[str] = []
        matched = False
        for line in raw_lines:
            if not line.strip():
                # preserve blank lines verbatim
                out_lines.append(line)
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                # Fix I-jsonl-loss: keep malformed lines verbatim, never drop them.
                out_lines.append(line)
                continue
            if not matched and _row_matches_target(d, target_item):
                d["resolved"] = new_status
                matched = True
            out_lines.append(json.dumps(d, ensure_ascii=False))
        if not matched:
            return False

        tmp = p.with_suffix(f".jsonl.{os.getpid()}.tmp")
        try:
            tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            return False
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fh.close()
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
