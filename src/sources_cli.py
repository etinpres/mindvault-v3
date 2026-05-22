#!/usr/bin/env python3
"""MindVault v3 Sprint 16 — Multi-source 등록 CLI.

추가 indexing scope 를 `~/.claude/mindvault-v2/sources.json` 에 영구 등록.
Sprint 11 의 `MV2_EXTRA_MEMORY_DIRS` env var 와 union 으로 동작 — env 는 shell
session 동안만, config 는 영구.

사용:
  python3 sources_cli.py list
  python3 sources_cli.py add ~/my-other-project/memory
  python3 sources_cli.py remove ~/my-other-project/memory

config 형식:
  {"sources": ["/path/1", "/path/2"]}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DATA_DIR = Path("/Users/yonghaekim/.claude/mindvault-v2")
CONFIG_PATH = DATA_DIR / "sources.json"


def _resolve_cfg(config_path: Path | None) -> Path:
    """None 이면 module 의 CONFIG_PATH 동적 조회 — patch.object(sources_cli, 'CONFIG_PATH', ...)
    로 테스트가 격리 경로 주입 가능. default arg evaluation 의 1회성 회피.
    """
    return config_path if config_path is not None else CONFIG_PATH


def load_sources(config_path: Path | None = None) -> list[str]:
    config_path = _resolve_cfg(config_path)
    if not config_path.is_file():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    srcs = data.get("sources")
    if not isinstance(srcs, list):
        return []
    return [s for s in srcs if isinstance(s, str) and s]


def save_sources(srcs: list[str], config_path: Path | None = None) -> None:
    config_path = _resolve_cfg(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"sources": srcs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def cmd_list(config_path: Path | None = None) -> int:
    srcs = load_sources(_resolve_cfg(config_path))
    sys.stdout.write(
        json.dumps({"sources": srcs}, ensure_ascii=False) + "\n"
    )
    return 0


def _normalize(path: str) -> str:
    return str(Path(path).expanduser().absolute())


def cmd_add(path: str, config_path: Path | None = None) -> int:
    cfg = _resolve_cfg(config_path)
    p = _normalize(path)
    target = Path(p)
    if not target.is_dir():
        sys.stdout.write(
            json.dumps({"ok": False, "error": "not a directory", "path": p})
            + "\n"
        )
        return 1
    srcs = load_sources(cfg)
    if p in srcs:
        sys.stdout.write(
            json.dumps({"ok": True, "added": False, "path": p}) + "\n"
        )
        return 0
    srcs.append(p)
    save_sources(srcs, cfg)
    sys.stdout.write(
        json.dumps({"ok": True, "added": True, "path": p}) + "\n"
    )
    return 0


def cmd_remove(path: str, config_path: Path | None = None) -> int:
    cfg = _resolve_cfg(config_path)
    p = _normalize(path)
    srcs = load_sources(cfg)
    if p not in srcs:
        sys.stdout.write(
            json.dumps({"ok": True, "removed": False, "path": p}) + "\n"
        )
        return 0
    srcs.remove(p)
    save_sources(srcs, cfg)
    sys.stdout.write(
        json.dumps({"ok": True, "removed": True, "path": p}) + "\n"
    )
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        sys.stdout.write(
            json.dumps({"error": "usage: list|add <path>|remove <path>"}) + "\n"
        )
        return 1
    sub = sys.argv[1]
    if sub == "list":
        return cmd_list()
    if sub == "add" and len(sys.argv) >= 3:
        return cmd_add(sys.argv[2])
    if sub == "remove" and len(sys.argv) >= 3:
        return cmd_remove(sys.argv[2])
    sys.stdout.write(json.dumps({"error": "bad args"}) + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
