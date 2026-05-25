#!/usr/bin/env python3
"""Sprint 9 — BGE-M3 vs Arctic-ko A/B raw cosine 분포 비교 [HISTORICAL].

⚠️  [DEPRECATED — historical reference] Sprint 14 에서 Arctic-ko 정식 채택 후
    BGE-M3 launchd 서비스 (com.mindvault.bge-m3) 는 제거. 현재 8081 포트는
    Arctic-ko 가 사용. 본 스크립트는 옛 측정 환경 재현 용이라 둘 다 살아있어야
    의미 있음. 그대로 돌리면 BGE_M3_URL=8081 이 사실은 Arctic-ko 를 부르고,
    ARCTIC_KO_URL=8082 는 connection refused → false comparison.

사용하려면:
1. scripts/bge_m3_server.py 를 별도 포트 (예: 18081) 로 수동 spin-up
2. 환경변수 override:
       MV3_EVAL_BGE_M3_URL=http://localhost:18081/embed \\
       MV3_EVAL_ARCTIC_KO_URL=http://localhost:8081/embed \\
       python3 src/eval_arctic_ko_ab.py

eval_top3_domain.py의 RELEVANT_QUERIES 10개 + 잡담 NOISE 10개를
두 서버에 각각 보내, 동일 memory/*.md 코퍼스에 대한 raw cosine 분포 차이 측정.

목표 (당시):
- 도메인 쿼리 top1 cosine: BGE-M3 0.77~0.83 → Arctic-ko 어떻게 이동?
- 잡담 쿼리 top1 cosine: BGE-M3 0.65~0.75 → Arctic-ko 분리되는가?
- 두 분포 사이 gap이 벌어지면 새 cosine 게이트 후보 도출

주의: 두 모델은 임베딩 분포가 완전히 다르므로 동일 메모리 코퍼스를 양쪽 모델로
임베딩한 후 비교해야 함. Arctic-ko는 별도 임시 인덱스 구축(코드 내).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

# 경로 — Claude Code 가 cwd 마다 생성하는 모든 projects 슬롯의 memory 디렉토리.
def _discover_memory_dirs() -> list[Path]:
    # v3.2.7: env var 우선 — production state pollution 방지 패턴.
    import os as _os
    root = Path(_os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser()
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/memory") if p.is_dir())


MEMORY_DIRS = _discover_memory_dirs()

# 환경변수 override 가능 — Sprint 14 이후 두 서버 동시 가동이 환경 의존적이라 강제 분리.
BGE_M3_URL = os.environ.get("MV3_EVAL_BGE_M3_URL", "http://localhost:18081/embed")
ARCTIC_KO_URL = os.environ.get("MV3_EVAL_ARCTIC_KO_URL", "http://localhost:8081/embed")
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
    # Arctic-ko 서버는 "kind" 필드 사용 (query 시 "query: " prefix 자동 부착).
    # 옛 코드는 포트 "8082" 가 Arctic-ko 라는 hardcoded 가정. Sprint 14 후
    # 포트 가변 → URL 이 ARCTIC_KO_URL 과 정확히 일치할 때만 kind 전송.
    if server_url == ARCTIC_KO_URL:
        body["kind"] = kind
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


def _server_alive(url: str) -> bool:
    health = url.rsplit("/", 1)[0] + "/health"
    try:
        with urllib.request.urlopen(health, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def run_ab() -> None:
    files = collect_memory_files()
    print(f"corpus: {len(files)} memory files")
    print(f"  BGE_M3_URL   = {BGE_M3_URL}")
    print(f"  ARCTIC_KO_URL= {ARCTIC_KO_URL}")
    if BGE_M3_URL == ARCTIC_KO_URL:
        print(
            "  ! 두 URL 이 같음 — A/B 비교 의미 없음. 각각 별도 서버 URL 지정 필요 "
            "(MV3_EVAL_BGE_M3_URL / MV3_EVAL_ARCTIC_KO_URL).",
            file=sys.stderr,
        )
        sys.exit(2)
    if not _server_alive(BGE_M3_URL):
        print(f"  ! BGE-M3 서버 ({BGE_M3_URL}) 응답 없음 — scripts/bge_m3_server.py 수동 spin-up 후 재시도",
              file=sys.stderr)
        sys.exit(3)
    if not _server_alive(ARCTIC_KO_URL):
        print(f"  ! Arctic-ko 서버 ({ARCTIC_KO_URL}) 응답 없음",
              file=sys.stderr)
        sys.exit(3)

    print("\n[1/2] BGE-M3 indexing")
    bge_corpus = index_corpus(BGE_M3_URL, files)
    print("\n[2/2] Arctic-ko indexing")
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
