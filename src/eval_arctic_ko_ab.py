#!/usr/bin/env python3
"""Sprint 9 — BGE-M3(8081) vs Arctic-ko(8082) A/B raw cosine 분포 비교.

eval_top3_domain.py의 RELEVANT_QUERIES 10개 + 잡담 NOISE 10개를
두 서버에 각각 보내, 동일 memory/*.md 코퍼스에 대한 raw cosine 분포 차이 측정.

목표:
- 도메인 쿼리 top1 cosine: BGE-M3 0.77~0.83 → Arctic-ko 어떻게 이동?
- 잡담 쿼리 top1 cosine: BGE-M3 0.65~0.75 → Arctic-ko 분리되는가?
- 두 분포 사이 gap이 벌어지면 새 cosine 게이트 후보 도출

주의: 두 모델은 임베딩 분포가 완전히 다르므로 동일 메모리 코퍼스를 양쪽 모델로
임베딩한 후 비교해야 함. Arctic-ko는 별도 임시 인덱스 구축(코드 내).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

# 경로 — Claude Code 가 cwd 마다 생성하는 모든 projects 슬롯의 memory 디렉토리.
def _discover_memory_dirs() -> list[Path]:
    root = Path("~/.claude/projects").expanduser()
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/memory") if p.is_dir())


MEMORY_DIRS = _discover_memory_dirs()

BGE_M3_URL = "http://localhost:8081/embed"
ARCTIC_KO_URL = "http://localhost:8082/embed"
TIMEOUT = 15

# 사용자가 실제로 회수하려는 도메인 (eval_top3_domain.py에서 복사)
RELEVANT_QUERIES = [
    "메일 보내는 sendmail SMTP 설정",
    "EPSON 스캐너 CLI 자동 크롭",
    "유튜브 영상 카드뉴스 제작",
    "MindVault 임베딩 게이트 sprint",
    "Karpathy LLM Wiki RAG 정리",
    "Higgsfield product photoshoot 프롬프트",
    "Assemble landing 페이지 다크 테마",
    "grammar saas 중학 영문법 망각곡선",
    "Hyperframes 비디오 렌더 fonts",
    "텔레그램 bot tg_notify 자동 응답",
]

# 잡담/메타 — 메모리에 매칭되면 안 됨
NOISE_QUERIES = [
    "안녕 잘 지냈어",
    "오늘 점심 뭐 먹지",
    "고마워",
    "ok 다음",
    "그래 알겠어",
    "이거 어떻게 생각해",
    "음 그렇구나",
    "잠깐만",
    "다시 해줘",
    "확인했어",
]


def embed(text: str, server_url: str, kind: str = "passage") -> list[float] | None:
    body = {"input": text}
    if "8082" in server_url:
        body["kind"] = kind  # Arctic-ko만 kind 필드 지원
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        server_url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())["vector"]
    except Exception as e:
        print(f"  ! embed fail [{server_url}]: {e}", file=sys.stderr)
        return None


def cosine(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def collect_memory_files() -> list[Path]:
    files = []
    for d in MEMORY_DIRS:
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            if any(part == "_staged" for part in p.parts):
                continue
            if p.name == "MEMORY.md":
                continue
            files.append(p)
    return files


def index_corpus(server_url: str, files: list[Path]) -> dict[Path, list[float]]:
    """memory/*.md 본문을 해당 서버로 임베딩. kind='passage'."""
    out = {}
    print(f"  indexing {len(files)} files via {server_url} ...")
    t0 = time.time()
    for i, p in enumerate(files, 1):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        vec = embed(text, server_url, kind="passage")
        if vec is not None:
            out[p] = vec
        if i % 20 == 0:
            print(f"   {i}/{len(files)} ({time.time()-t0:.1f}s)")
    print(f"  done: {len(out)} embedded in {time.time()-t0:.1f}s")
    return out


def top1_cosine(query: str, server_url: str, corpus: dict[Path, list[float]]) -> tuple[float, Path | None]:
    qvec = embed(query, server_url, kind="query")
    if qvec is None or not corpus:
        return 0.0, None
    best = (0.0, None)
    for path, dvec in corpus.items():
        c = cosine(qvec, dvec)
        if c > best[0]:
            best = (c, path)
    return best


def run_ab() -> None:
    files = collect_memory_files()
    print(f"corpus: {len(files)} memory files")

    print("\n[1/2] BGE-M3 (8081) indexing")
    bge_corpus = index_corpus(BGE_M3_URL, files)
    print("\n[2/2] Arctic-ko (8082) indexing")
    arc_corpus = index_corpus(ARCTIC_KO_URL, files)

    print("\n" + "=" * 72)
    print(f"{'QUERY':<40s}  {'BGE-M3':>8s}  {'Arctic':>8s}  {'Δ':>6s}  HIT")
    print("=" * 72)

    results = {"relevant": {"bge": [], "arc": []}, "noise": {"bge": [], "arc": []}}

    print("\n[RELEVANT — 메모리 hit 기대]")
    for q in RELEVANT_QUERIES:
        b_c, b_p = top1_cosine(q, BGE_M3_URL, bge_corpus)
        a_c, a_p = top1_cosine(q, ARCTIC_KO_URL, arc_corpus)
        same = "=" if b_p == a_p else "≠"
        results["relevant"]["bge"].append(b_c)
        results["relevant"]["arc"].append(a_c)
        hit = (a_p.stem if a_p else "?")[:18]
        print(f"  {q[:38]:<40s}  {b_c:>8.4f}  {a_c:>8.4f}  {a_c-b_c:>+6.3f}  {same} {hit}")

    print("\n[NOISE — 메모리 miss 기대]")
    for q in NOISE_QUERIES:
        b_c, b_p = top1_cosine(q, BGE_M3_URL, bge_corpus)
        a_c, a_p = top1_cosine(q, ARCTIC_KO_URL, arc_corpus)
        same = "=" if b_p == a_p else "≠"
        results["noise"]["bge"].append(b_c)
        results["noise"]["arc"].append(a_c)
        hit = (a_p.stem if a_p else "?")[:18]
        print(f"  {q[:38]:<40s}  {b_c:>8.4f}  {a_c:>8.4f}  {a_c-b_c:>+6.3f}  {same} {hit}")

    print("\n" + "=" * 72)
    print("분포 요약:")

    def summary(label, arr):
        a = np.array(arr)
        return f"  {label:<24s} mean={a.mean():.4f}  median={np.median(a):.4f}  min={a.min():.4f}  max={a.max():.4f}"

    print(summary("BGE-M3 RELEVANT top1", results["relevant"]["bge"]))
    print(summary("Arctic-ko RELEVANT top1", results["relevant"]["arc"]))
    print(summary("BGE-M3 NOISE top1", results["noise"]["bge"]))
    print(summary("Arctic-ko NOISE top1", results["noise"]["arc"]))

    bge_gap = np.mean(results["relevant"]["bge"]) - np.mean(results["noise"]["bge"])
    arc_gap = np.mean(results["relevant"]["arc"]) - np.mean(results["noise"]["arc"])
    print(f"\n  BGE-M3 분리 gap (RELEVANT mean - NOISE mean): {bge_gap:+.4f}")
    print(f"  Arctic-ko 분리 gap:                          {arc_gap:+.4f}")
    print(f"  → Arctic-ko gap이 더 크면 false negative 회색지대 분리에 우위")

    # 게이트 후보: NOISE max + safety < gate < RELEVANT min - safety
    arc_noise_max = max(results["noise"]["arc"])
    arc_relevant_min = min(results["relevant"]["arc"])
    print(f"\n  Arctic-ko 게이트 후보 범위: ({arc_noise_max:.4f}, {arc_relevant_min:.4f})")
    if arc_noise_max < arc_relevant_min:
        suggested = (arc_noise_max + arc_relevant_min) / 2
        print(f"    → 중간값 {suggested:.4f}로 설정 시 100% 분리 가능")
    else:
        print(f"    ⚠ overlap 발생 — 임베딩 모델만으로는 분리 불가, reranker 검토")


if __name__ == "__main__":
    run_ab()
