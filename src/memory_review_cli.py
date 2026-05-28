#!/usr/bin/env python3
"""MindVault v3 Sprint 3 — /memory review CLI.

하위 명령:
  list                          → staged 후보 JSON 출력 (update 후보면 update_of 표시)
  diff <filename> [--pretty]    → Sprint 14: update 후보의 기존 vs 정제 본문 unified diff.
                                  Sprint NEXT-5: --pretty 또는 tty 자동 감지 시 ANSI 색상.
                                  --pretty 명시 시 plain text (JSON 아님)
  approve <filename>            → staged → memory/ 이동 + MEMORY.md 한 줄 append.
                                  Sprint 14: update_of 메타 있으면 기존 .bak 백업 + body overwrite
  reject  <filename>            → staged 파일 삭제
  prune                         → 30일 경과 staged 삭제
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path


def _default_memory_dir() -> Path:
    """현재 사용자 $HOME 으로부터 Claude Code 프로젝트 슬롯의 memory/ 파생.

    v3.2.8: env override 우선순위 — close-session.md skill 과 통일.
    (1) `MV3_MEMORY_DIR` — skill 과 동일 변수, 이게 MEMORY_DIR 자체.
    (2) `MV3_PROJECTS_DIR` — 슬롯 root, MEMORY_DIR = $/memory.
    (3) home_slug default.
    """
    mem_override = os.environ.get("MV3_MEMORY_DIR", "").strip()
    if mem_override:
        return Path(mem_override).expanduser()
    proj_override = os.environ.get("MV3_PROJECTS_DIR", "").strip()
    if proj_override:
        return Path(proj_override).expanduser() / "memory"
    home_slug = "-" + str(Path.home()).strip("/").replace("/", "-")
    root = Path(os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser()
    return root / home_slug / "memory"


# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
_MV3_DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
MEMORY_DIR = _default_memory_dir()
PROJECTS_DIR = MEMORY_DIR.parent
STAGED_DIR = MEMORY_DIR / "_staged"
# Sprint 13: procedural slot 분리. list/approve/reject/prune 모두 양쪽 staged
# 디렉토리 스캔. promoted target 은 type 별로 분기 (procedural → PROCEDURAL_DIR).
PROCEDURAL_DIR = MEMORY_DIR / "_procedural"
PROCEDURAL_STAGED_DIR = PROCEDURAL_DIR / "_staged"
STAGED_DIRS = (STAGED_DIR, PROCEDURAL_STAGED_DIR)
INDEX_MD = MEMORY_DIR / "MEMORY.md"
DEBUG_LOG = _MV3_DATA_DIR / "debug.log"
STAGED_TTL_DAYS = 30

# Sprint NEXT-5 — ANSI 색상 diff 출력. 사용자가 매 update 검토 시 +/- 식별 비용 ↓.
ANSI_RESET = "\033[0m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_MAGENTA = "\033[35m"
ANSI_BOLD_BLUE = "\033[1;34m"


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] review: {msg}\n")
    except Exception:
        pass


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_raw = parts[1]
    body = parts[2].lstrip("\n")
    meta = {}
    for line in fm_raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


def _iter_staged_files():
    """양쪽 staged 디렉토리에서 .md 순회. 시간 안정 정렬."""
    files: list[Path] = []
    for d in STAGED_DIRS:
        if d.is_dir():
            files.extend(d.glob("*.md"))
    files.sort(key=lambda p: p.name)
    return files


def cmd_list() -> int:
    items = []
    now = time.time()
    for f in _iter_staged_files():
        try:
            text = f.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(text)
            age_days = int((now - f.stat().st_mtime) // 86400)
            items.append(
                {
                    "file": f.name,
                    "type": meta.get("type", "feedback"),
                    "title": meta.get("name", f.stem),
                    "body": body.strip(),
                    "reason": meta.get("reason", ""),
                    "evidence": meta.get("evidence", ""),
                    "staged_at": meta.get("staged_at", ""),
                    "age_days": age_days,
                }
            )
        except OSError as e:
            _debug(f"list read fail {f.name}: {e}")
    sys.stdout.write(json.dumps({"staged": items}, ensure_ascii=False))
    return 0


def _promoted_slug(staged_name: str) -> str:
    # 20260415-120000_feedback_no_mocks.md → no_mocks
    stem = Path(staged_name).stem
    m = re.match(r"\d{8}-\d{6}_[a-z]+_(.+)$", stem)
    return m.group(1) if m else stem


def _safe_staged_path(filename: str) -> Path | None:
    """filename이 STAGED_DIRS 중 하나 안의 단일 md 파일인지 검증. path traversal 차단.

    Sprint 13: 양쪽 staged 슬롯 lookup. 동일 filename 이 양쪽에 있을 일은 없지만
    (slugify 결과 + timestamp 결합 → 충돌 가능성 무시), 둘 다 있으면 결정 슬롯 우선.
    """
    if not filename or filename != Path(filename).name or not filename.endswith(".md"):
        return None
    for d in STAGED_DIRS:
        p = d / filename
        if p.is_file():
            return p
    return None


def _promote_target_dir(meta_type: str) -> Path:
    """type 별 promote 대상 디렉토리. procedural 만 _procedural/, 그 외 root."""
    if meta_type == "procedural":
        return PROCEDURAL_DIR
    return MEMORY_DIR


def _allowed_update_roots() -> list[Path]:
    """update_of safety check 용 root 목록. memory_indexer 의 정책과 동일."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from memory_indexer import DEFAULT_MEMORY_DIRS, _extra_memory_dirs
        return [*DEFAULT_MEMORY_DIRS, *_extra_memory_dirs()]
    except Exception:
        # fallback — 현재 모듈의 MEMORY_DIR 만 허용
        return [MEMORY_DIR]


def _is_safe_update_target(target: Path) -> bool:
    """target 이 허용된 메모리 root 의 하위인지 (symlink resolve 후) 검증."""
    try:
        resolved = target.resolve(strict=False)
    except OSError:
        return False
    for root in _allowed_update_roots():
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def _supersede_passthrough(meta: dict) -> str:
    """Defect Suspect2: preserve ``supersedes:`` / ``deprecated_by:`` audit links.

    cmd_approve rebuilds frontmatter from scratch (keeping only
    name/description/type), which silently DROPPED the ``supersedes: [...]``
    injected by ``contradiction_review_cli resolve --action supersede`` into a
    STAGED file — losing the NEW→OLD audit link on promote.

    parse_frontmatter stores list values as the raw ``[a, b]`` STRING (no YAML
    parse), so we re-emit them verbatim. Returns trailing-newline-terminated
    extra frontmatter lines (or "" if neither key is present).
    """
    extra = ""
    for key in ("supersedes", "deprecated_by"):
        val = meta.get(key)
        if val is None:
            continue
        val = str(val).strip()
        if not val:
            continue
        extra += f"{key}: {val}\n"
    return extra


def _read_existing_body(path: Path) -> str:
    """기존 memory 파일에서 본문(frontmatter 제외) 추출. 없으면 빈 문자열."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    _, body = parse_frontmatter(text)
    return body or ""


def _colorize_diff(diff_text: str) -> str:
    """unified diff 라인별 ANSI 색상 적용. Sprint NEXT-5.

    - `+` 라인: green (단 `+++` 헤더는 bold blue)
    - `-` 라인: red (단 `---` 헤더는 bold blue)
    - `@@` hunk 헤더: magenta
    - 그 외 컨텍스트 라인: 무색
    """
    out: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out.append(f"{ANSI_BOLD_BLUE}{line}{ANSI_RESET}")
        elif line.startswith("@@"):
            out.append(f"{ANSI_MAGENTA}{line}{ANSI_RESET}")
        elif line.startswith("+"):
            out.append(f"{ANSI_GREEN}{line}{ANSI_RESET}")
        elif line.startswith("-"):
            out.append(f"{ANSI_RED}{line}{ANSI_RESET}")
        else:
            out.append(line)
    return "\n".join(out)


def _should_use_color(pretty_flag: bool) -> bool:
    """tty 자동 감지 + --pretty 강제. pretty_flag True 면 무조건 색상.

    False 면 isatty 자동 판단도 안 함 — JSON 출력 안에 ANSI 섞이면 파싱 불편.
    색상은 항상 명시 opt-in (--pretty) 으로 둔다.
    """
    return bool(pretty_flag)


def cmd_diff(filename: str, pretty: bool = False) -> int:
    """Sprint 14: staged 후보가 update_of 가지면 기존 vs 정제 unified diff 출력.

    update_of 없으면 신규 후보임을 알리고 staged 본문 표시.
    Sprint NEXT-5: pretty=True 면 JSON 대신 ANSI 색상 plain text.
    """
    src = _safe_staged_path(filename)
    if src is None:
        sys.stdout.write(json.dumps({"ok": False, "error": "invalid filename"}))
        return 0
    if not src.is_file():
        sys.stdout.write(json.dumps({"ok": False, "error": "not found"}))
        return 0
    try:
        text = src.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        update_of = (meta.get("update_of") or "").strip()
        use_color = _should_use_color(pretty)
        if not update_of:
            if pretty:
                sys.stdout.write(
                    f"[new] {meta.get('name', src.stem)}\n\n{body.strip()}\n"
                )
            else:
                sys.stdout.write(json.dumps({
                    "ok": True,
                    "kind": "new",
                    "title": meta.get("name", src.stem),
                    "body": body.strip(),
                }, ensure_ascii=False))
            return 0
        target = Path(update_of)
        if not _is_safe_update_target(target):
            err = {"ok": False, "error": "unsafe update target", "target": update_of}
            if pretty:
                sys.stdout.write(f"error: unsafe update target\n  {update_of}\n")
            else:
                sys.stdout.write(json.dumps(err))
            return 0
        existing = _read_existing_body(target) if target.is_file() else ""
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            from memory_compiler import unified_diff_text
            diff = unified_diff_text(existing, body)
        except Exception as e:
            _debug(f"diff render fail: {e}")
            diff = ""
        if pretty:
            colored = _colorize_diff(diff) if use_color else diff
            header = (
                f"[update] {meta.get('name', src.stem)}\n"
                f"  target: {update_of}\n"
                f"  summary: {meta.get('diff_summary', '')}\n"
                f"  {len(existing)} → {len(body)} chars\n\n"
            )
            sys.stdout.write(header + colored + "\n")
        else:
            sys.stdout.write(json.dumps({
                "ok": True,
                "kind": "update",
                "title": meta.get("name", src.stem),
                "update_of": update_of,
                "diff_summary": meta.get("diff_summary", ""),
                "existing_len": len(existing),
                "compiled_len": len(body),
                "unified_diff": diff,
            }, ensure_ascii=False))
        return 0
    except Exception as e:
        _debug(f"diff FATAL {filename}: {e}")
        if pretty:
            sys.stdout.write(f"error: {e}\n")
        else:
            sys.stdout.write(json.dumps({"ok": False, "error": str(e)}))
        return 0


def cmd_approve(filename: str) -> int:
    src = _safe_staged_path(filename)
    if src is None:
        sys.stdout.write(json.dumps({"ok": False, "error": "invalid filename"}))
        return 0
    if not src.is_file():
        sys.stdout.write(json.dumps({"ok": False, "error": "not found"}))
        return 0
    try:
        text = src.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        slug = _promoted_slug(filename)
        meta_type = meta.get("type", "feedback")
        update_of = (meta.get("update_of") or "").strip()

        # Sprint 14: update flow — 기존 파일 백업 후 overwrite. 기존 frontmatter 의
        # name/description/type 보존, body 만 정제본으로 교체. INDEX_MD 추가 append
        # 안 함 (slug 이미 존재).
        if update_of:
            target = Path(update_of)
            if not _is_safe_update_target(target):
                sys.stdout.write(json.dumps({
                    "ok": False,
                    "error": "unsafe update target",
                    "target": update_of,
                }))
                return 0
            if not target.is_file():
                # 기존 파일이 사라졌다면 신규 promotion 으로 fallback
                update_of = ""
            else:
                bak = target.with_suffix(target.suffix + ".bak")
                # v3.2.6 Round 3 NR3: atomic backup — overwrite 직전 partial .bak
                # 잔류 시 향후 rollback 시도 실패. tmp + os.replace.
                # v3.2.8: finally — KeyboardInterrupt 등 BaseException 도 tmp orphan 차단.
                _bak_tmp = target.with_suffix(target.suffix + ".bak.tmp")
                try:
                    _bak_tmp.write_text(
                        target.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    os.replace(_bak_tmp, bak)
                except OSError as e:
                    _debug(f"backup write fail {target}: {e}")
                    sys.stdout.write(json.dumps({
                        "ok": False, "error": f"backup fail: {e}",
                    }))
                    return 0
                finally:
                    try:
                        _bak_tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                existing_meta, _ = parse_frontmatter(
                    target.read_text(encoding="utf-8")
                )
                # Defect Suspect2: passthrough supersedes/deprecated_by audit
                # links from the STAGED frontmatter (where supersede injects them)
                # AND the existing target (preserve any prior links). Staged wins.
                passthrough_meta = {
                    **{k: existing_meta[k] for k in ("supersedes", "deprecated_by") if k in existing_meta},
                    **{k: meta[k] for k in ("supersedes", "deprecated_by") if k in meta},
                }
                final_fm = (
                    "---\n"
                    f"name: {existing_meta.get('name', meta.get('name', slug))}\n"
                    f"description: {existing_meta.get('description', meta.get('description', slug))}\n"
                    f"type: {existing_meta.get('type', meta_type)}\n"
                    f"{_supersede_passthrough(passthrough_meta)}"
                    "---\n\n"
                    f"{body.rstrip()}\n"
                )
                # v3.2.6 Round 2 NR1: atomic write — approve 가 영구 메모리를
                # overwrite 하는 critical path. partial 잔류 시 다음 hook recall
                # 이 broken frontmatter parse 실패.
                # v3.2.8: finally — KeyboardInterrupt 도 tmp orphan 차단.
                _tmp = target.with_suffix(target.suffix + ".tmp")
                try:
                    _tmp.write_text(final_fm, encoding="utf-8")
                    os.replace(_tmp, target)
                finally:
                    try:
                        _tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                src.unlink()
                reindex_info: dict = {}
                try:
                    from memory_indexer import incremental_index  # noqa: WPS433
                    reindex_info = incremental_index()
                except Exception as e:
                    _debug(f"approve update reindex skip: {type(e).__name__}: {e}")
                    reindex_info = {"skipped": "reindex failed", "error": str(e)}
                sys.stdout.write(json.dumps({
                    "ok": True,
                    "kind": "update",
                    "target": str(target),
                    "backup": str(bak),
                    "reindex": reindex_info,
                }, ensure_ascii=False))
                return 0

        target_dir = _promote_target_dir(meta_type)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{slug}.md"
        if target.exists():
            sys.stdout.write(json.dumps({"ok": False, "error": "target exists", "target": str(target)}))
            return 0

        final_fm = (
            "---\n"
            f"name: {meta.get('name', slug)}\n"
            f"description: {meta.get('description', meta.get('name', slug))}\n"
            f"type: {meta_type}\n"
            # Defect Suspect2: passthrough supersedes/deprecated_by from staged fm.
            f"{_supersede_passthrough(meta)}"
            "---\n\n"
            f"{body.rstrip()}\n"
        )
        # v3.2.6 Round 2 NR1: atomic write — 신규 promote 도 동일 패턴.
        # v3.2.8: finally — KeyboardInterrupt 도 tmp orphan 차단.
        _tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            _tmp.write_text(final_fm, encoding="utf-8")
            os.replace(_tmp, target)
        finally:
            try:
                _tmp.unlink(missing_ok=True)
            except OSError:
                pass

        if INDEX_MD.is_file():
            line = f"- [{meta.get('name', slug)}]({slug}.md) — {meta.get('reason', '')}\n"
            existing = INDEX_MD.read_text(encoding="utf-8")
            prefix = "" if existing.endswith("\n") else "\n"
            with INDEX_MD.open("a", encoding="utf-8") as f:
                f.write(prefix + line)

        src.unlink()

        # Sprint 4: 새 메모리 즉시 임베딩 인덱싱 (실패해도 staged 작업은 성공)
        reindex_info: dict = {}
        try:
            from memory_indexer import incremental_index  # noqa: WPS433
            reindex_info = incremental_index()
        except Exception as e:
            _debug(f"approve reindex skip: {type(e).__name__}: {e}")
            reindex_info = {"skipped": "reindex failed", "error": str(e)}

        sys.stdout.write(json.dumps(
            {"ok": True, "target": str(target), "reindex": reindex_info},
            ensure_ascii=False,
        ))
        return 0
    except Exception as e:
        _debug(f"approve FATAL {filename}: {e}")
        sys.stdout.write(json.dumps({"ok": False, "error": str(e)}))
        return 0


def cmd_reject(filename: str) -> int:
    src = _safe_staged_path(filename)
    if src is None:
        sys.stdout.write(json.dumps({"ok": False, "error": "invalid filename"}))
        return 0
    if not src.is_file():
        sys.stdout.write(json.dumps({"ok": False, "error": "not found"}))
        return 0
    try:
        src.unlink()
        sys.stdout.write(json.dumps({"ok": True}))
        return 0
    except OSError as e:
        sys.stdout.write(json.dumps({"ok": False, "error": str(e)}))
        return 0


def cmd_prune() -> int:
    cutoff = time.time() - STAGED_TTL_DAYS * 86400
    removed = 0
    for d in STAGED_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    sys.stdout.write(json.dumps({"removed": removed}))
    return 0


def main() -> int:
    try:
        if len(sys.argv) < 2:
            sys.stdout.write(json.dumps({"error": "usage: list|diff|approve|reject|prune"}))
            return 0
        sub = sys.argv[1]
        # Sprint NEXT-5: argv 에서 --pretty 분리
        rest = [a for a in sys.argv[2:] if a != "--pretty"]
        pretty = "--pretty" in sys.argv[2:]
        if sub == "list":
            return cmd_list()
        if sub == "diff" and len(rest) >= 1:
            return cmd_diff(rest[0], pretty=pretty)
        if sub == "approve" and len(rest) >= 1:
            return cmd_approve(rest[0])
        if sub == "reject" and len(rest) >= 1:
            return cmd_reject(rest[0])
        if sub == "prune":
            return cmd_prune()
        sys.stdout.write(json.dumps({"error": "bad args"}))
        return 0
    except Exception as e:
        _debug(f"main FATAL: {e}\n{traceback.format_exc()}")
        sys.stdout.write(json.dumps({"ok": False, "error": "fatal"}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
