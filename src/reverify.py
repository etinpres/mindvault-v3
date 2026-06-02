#!/usr/bin/env python3
"""MindVault v3 Phase 1 ③ — 신뢰성 검증 (stale 자동 감지, over-trust 해소).

메모리의 코드/사실 참조(모델명·포트)를 현행 코드와 결정론적으로 대조해 stale
의심을 판정한다. Layer 5 모순감지(memory vs memory)와 달리 ③은 memory vs 현행
코드. Gemma 미사용 — 운영비 0, 결정론, CI pin 가능.

판별 신호(설계 §2): 메모리가 stale_alias 토큰을 포함하면서 current_value 토큰을
미포함하면 stale 의심. 현행 값을 함께 언급하는 이력 메모리는 면제. verifier 가
current_value 가 라이브 코드에 실재하는지 확인 → registry 자체의 메타-staleness 차단.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


def default_root() -> Path:
    """현행 코드 ground truth root. MV3_REVERIFY_ROOT env 우선.

    env 미지정 시 layout 자동 판별 (audit: flat 배포 overshoot 차단):
      - repo  : src/reverify.py → parent.parent(repo root) 에 src/memory_indexer.py 존재
      - flat 배포: ~/.claude/scripts/mindvault/reverify.py → 같은 dir 에 memory_indexer.py
        sibling → root=그 dir (verifier 의 basename 폴백이 root/memory_indexer.py 매칭)
    verifier 가 grep 하는 rel_path("src/memory_indexer.py")가 root 기준 1차 또는
    basename 폴백으로 둘 다에서 resolve 되도록 root 를 고른다.
    """
    env = os.environ.get("MV3_REVERIFY_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve().parent
    # repo layout: repo_root/src/memory_indexer.py
    if (here.parent / "src" / "memory_indexer.py").is_file():
        return here.parent
    # flat 배포 layout: reverify.py 와 memory_indexer.py 가 같은 dir
    if (here / "memory_indexer.py").is_file():
        return here
    return here.parent  # fallback (dev/repo 가정)


def _grep_present(root: Path, rel_path: str, pattern: str) -> bool:
    """root/rel_path (없으면 root/basename — flat 배포 layout) 에 pattern 존재하면 True."""
    candidates = [root / rel_path, root / Path(rel_path).name]
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


@dataclass(frozen=True)
class CanonicalFact:
    key: str
    current_value: str            # 현행 진실 토큰 (회수 메모리가 이걸 언급하면 면제)
    stale_aliases: tuple[str, ...]  # 현재처럼 주장되면 stale 인 옛 토큰들
    verifier: Callable            # (root: Path) -> bool : current_value 가 라이브?
    description: str = ""


# 초기 facts — 실측 stale 위험 + verifier 라이브 통과 확인 (설계 D3).
# 확장: 형이 summarizer 포트·버전·파일경로 등 한 줄씩 추가 (단 verifier 라이브 통과 필수).
CANONICAL_FACTS = (
    CanonicalFact(
        key="embedding_model",
        current_value="arctic",
        stale_aliases=("bge-m3", "bge_m3", "bge m3"),
        verifier=lambda root: _grep_present(root, "src/memory_indexer.py", r"arctic"),
        description="임베딩 모델 (Sprint 9/14 BGE-M3 → Arctic-ko 교체)",
    ),
    CanonicalFact(
        key="embedding_port",
        current_value="8081",
        stale_aliases=("8765",),
        verifier=lambda root: _grep_present(root, "src/memory_indexer.py", r"(?<!\d)8081(?!\d)"),
        description="임베딩 서버 포트 (Arctic-ko :8081)",
    ),
)


def _contains_token(text: str, token: str) -> bool:
    """대소문자 무시 토큰 포함 검사. 라틴 영숫자 경계로 부분어 오매칭 차단.

    경계 `(?<![A-Za-z0-9])tok(?![A-Za-z0-9])` 는 라틴 알파벳/숫자 이웃만 막고
    한국어/구두점 인접은 허용한다 → 'arctic임베딩'·'arctic-ko' 는 매칭(포함),
    'subarctic'·'18081' 안의 토큰은 비매칭(오탐 차단). 숫자·하이픈·언더스코어·
    공백 포함 토큰 모두 동일 규칙. \\b 는 한국어를 \\w 로 봐 'arctic임' 인접을
    잘못 끊으므로 쓰지 않는다.
    """
    if not token:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9]){re.escape(token.lower())}(?![A-Za-z0-9])", text.lower()
    ) is not None


@dataclass
class StaleVerdict:
    status: str                   # "stale" | "fresh"
    note: str = ""
    findings: List[str] = field(default_factory=list)


def check_memory_staleness(
    text: str, root: Optional[Path] = None, facts: tuple = CANONICAL_FACTS
) -> StaleVerdict:
    """메모리 텍스트(frontmatter+body)를 현행 코드와 대조해 stale 판정 (설계 §2).

    각 fact 에 대해: verifier(root) 가 current_value 라이브 확인 못 하면 skip
    (registry stale 의심 → verify_registry). current_value 토큰 동반이면 면제(이력).
    stale_alias 토큰만 있으면 → finding 누적. finding 있으면 stale.
    """
    if root is None:
        root = default_root()
    if not text:
        return StaleVerdict(status="fresh")
    findings: List[str] = []
    for fact in facts:
        if not fact.verifier(root):
            continue  # current_value 라이브 확인 불가 → 이 fact 로 판정 안 함
        if _contains_token(text, fact.current_value):
            continue  # 현행 값 동반 → 정당한 이력/현행, 면제
        hit = next((a for a in fact.stale_aliases if _contains_token(text, a)), None)
        if hit:
            findings.append(
                f"{fact.key} 현재형 참조 {hit} (현행 {fact.current_value} 미언급)"
            )
    if findings:
        return StaleVerdict(status="stale", note="; ".join(findings)[:300], findings=findings)
    return StaleVerdict(status="fresh")


def verify_registry(root: Optional[Path] = None, facts: tuple = CANONICAL_FACTS) -> List[dict]:
    """각 fact 의 current_value 가 라이브 코드에 실재하는지 self-check.

    반환: verifier fail 한 fact 들 [{key, description}] — registry stale 경고용.
    빈 리스트 = 레지스트리 정상.
    """
    if root is None:
        root = default_root()
    return [
        {"key": f.key, "description": f.description}
        for f in facts
        if not f.verifier(root)
    ]


# 선택적 BOM 허용(^﻿?); 닫는 펜스 뒤 가로공백만 소비([ \t]*\r?\n?) → 본문 구분
# 빈 줄을 먹지 않음(audit BUG1). CRLF(\r?\n) 허용(audit BUG2).
_FM_RE = re.compile(r"^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_REVERIFY_KEYS = ("reverify_status", "reverify_checked", "reverify_note")
REVERIFY_INTERVAL_DAYS = 7


def _data_dir() -> Path:
    return Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()


def _sidecar_path() -> Path:
    return _data_dir() / "reverify_state.json"


def _oneline(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\r", " ").replace("\n", " ")).strip()


def upsert_reverify_frontmatter(text: str, status: str, note: str, checked: str) -> str:
    """frontmatter 에 reverify_* 키 upsert (순수 함수). 본문·기존 키 보존, reverify_* 만 교체.

    frontmatter 없으면 생성. note 는 단일 라인 정규화 (라인 파서 호환).
    """
    new_lines = [f"reverify_status: {status}", f"reverify_checked: {checked}"]
    note1 = _oneline(note)
    if note1:
        # note 를 JSON 으로 인용 — JSON 문자열은 유효한 YAML double-quoted scalar 라
        # 콜론(': ')·'#'·따옴표가 들어가도 yaml.safe_load(recall 경로)가 안 깨진다.
        # (audit: 미래 fact alias 에 ': ' 포함 시 frontmatter 전체 소실 차단)
        new_lines.append("reverify_note: " + json.dumps(note1, ensure_ascii=False))
    m = _FM_RE.match(text)
    if not m:
        return "---\n" + "\n".join(new_lines) + "\n---\n\n" + text
    kept = [
        ln.rstrip("\r") for ln in m.group(1).split("\n")
        if not any(ln.startswith(k + ":") for k in _REVERIFY_KEYS)
    ]
    merged = "\n".join(kept + new_lines)
    return "---\n" + merged + "\n---\n" + text[m.end():]


def _strip_reverify_frontmatter(text: str) -> str:
    """frontmatter 에서 reverify_* 키 제거 (stale→fresh cleanup). frontmatter 없으면 원본."""
    m = _FM_RE.match(text)
    if not m:
        return text
    kept = [
        ln.rstrip("\r") for ln in m.group(1).split("\n")
        if not any(ln.startswith(k + ":") for k in _REVERIFY_KEYS)
    ]
    return "---\n" + "\n".join(kept) + "\n---\n" + text[m.end():]


def _current_reverify_status(text: str) -> Optional[str]:
    m = _FM_RE.match(text)
    if not m:
        return None
    mm = re.search(r"^reverify_status:\s*(\S+)", m.group(1), re.MULTILINE)
    return mm.group(1) if mm else None


def _current_reverify_note(text: str) -> str:
    """frontmatter 의 reverify_note 값을 디코드해 반환.

    JSON 인용 형식(upsert 가 쓰는 현행)은 json.loads 로 풀고, 옛 비인용 노트는
    raw strip 로 back-compat. 디코드된 값이 _oneline(note) 와 같아야 idempotent.
    """
    m = _FM_RE.match(text)
    if not m:
        return ""
    mm = re.search(r"^reverify_note:\s*(.*)$", m.group(1), re.MULTILINE)
    if not mm:
        return ""
    raw = mm.group(1).strip()
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, str):
            return decoded
    except (ValueError, TypeError):
        pass
    return raw


def _read_raw(path: Path) -> Optional[str]:
    """파일을 universal-newline 변환 없이(newline="") 읽되 CRLF 는 보존, 단독 CR 만 LF 로.

    Path.read_text 는 \\r\\n 을 \\n 으로 접어서, stale write-back 시 본문 line
    ending 이 통째로 LF 로 바뀌는 부작용이 있었다(audit R1). 실패 시 None.

    단독 CR(\\r, classic-Mac, \\n 미동반)은 \\n 으로 정규화한다(audit R2): _FM_RE 가
    \\r?\\n 만 인식하므로, 정규화 안 하면 lone-CR frontmatter 를 scan 이 못 잡고
    reverify_cli list(read_text=universal-newline)와도 어긋난다. \\r\\n 은 그대로 둬
    CRLF 본문 보존(R1) 유지. lone-CR 은 2001년 이전 Mac 외엔 사실상 부재.
    """
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    return re.sub(r"\r(?!\n)", "\n", text)  # lone CR → LF (CRLF 은 보존)


def _atomic_write(path: Path, content: str) -> bool:
    # PID-unique tmp — 고정 ".tmp" 는 동시 SessionEnd(sibling Conductor workspaces)가
    # 같은 공유 메모리 파일/sidecar 에 쓸 때 한쪽 finally-unlink 가 다른쪽 tmp 를 지워
    # os.replace 실패→쓰기 silent 손실(audit R2). alias_generator/contradiction_review_cli
    # 동일 관례.
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        # newline="" — content 의 \r\n 을 그대로 디스크에 쓴다(본문 line ending 보존).
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def write_back_verdict(path: Path, verdict: StaleVerdict, checked: str) -> bool:
    """판정 결과를 파일 frontmatter 에 atomic 반영. 반환: 실제로 썼으면 True.

    - stale: status/note 변화 있을 때만 upsert (idempotent — 불변이면 checked churn 없이 skip).
    - fresh: 기존 flag 있으면 제거(cleanup), 없으면 no-op (fresh 메모리 무손상).
    """
    text = _read_raw(path)
    if text is None:
        return False
    cur_status = _current_reverify_status(text)
    if verdict.status == "stale":
        if _FM_RE.match(text) is None:
            return False  # frontmatter 없음/미인식 → 안전하게 skip (이중 FM 방지)
        if cur_status == "stale" and _current_reverify_note(text) == _oneline(verdict.note):
            return False  # idempotent
        return _atomic_write(
            path, upsert_reverify_frontmatter(text, "stale", verdict.note, checked)
        )
    # fresh
    if cur_status is None:
        return False  # 무flag fresh → no-op
    return _atomic_write(path, _strip_reverify_frontmatter(text))


def _collect_memory_files(mem_dir: Path) -> List[Path]:
    """*.md + _procedural/*.md, MEMORY.md·_staged 제외 (provenance_backfill 와 동일 범위)."""
    files: List[Path] = []
    for base in (mem_dir, mem_dir / "_procedural"):
        if not base.is_dir():
            continue
        for p in base.glob("*.md"):
            if p.name == "MEMORY.md" or any(part == "_staged" for part in p.parts):
                continue
            files.append(p)
    return sorted(files)


def scan_memories(
    mem_dir: Path, root: Optional[Path] = None, checked: Optional[str] = None
) -> dict:
    """mem_dir 의 모든 메모리를 현행 코드와 대조 + frontmatter flag 갱신.

    반환: {flagged, cleared, checked(=처리 파일수), total}. sidecar last_scan 갱신.
    """
    if root is None:
        root = default_root()
    if checked is None:
        checked = time.strftime("%Y-%m-%d")
    flagged = cleared = processed = 0
    files = _collect_memory_files(mem_dir)
    for p in files:
        text = _read_raw(p)
        if text is None:
            continue
        processed += 1
        verdict = check_memory_staleness(_strip_reverify_frontmatter(text), root)
        had_flag = _current_reverify_status(text) is not None
        wrote = write_back_verdict(p, verdict, checked)
        if verdict.status == "stale" and wrote:
            flagged += 1
        elif verdict.status == "fresh" and had_flag and wrote:
            cleared += 1
    _write_sidecar()
    return {"flagged": flagged, "cleared": cleared, "processed": processed, "total": len(files)}


def _read_sidecar_last_scan() -> Optional[float]:
    try:
        d = json.loads(_sidecar_path().read_text(encoding="utf-8"))
        # bug-audit 2026-06-01 (reverify-nonentdict-sidecar): 손상 sidecar 가 비-dict
        # (`[]`/`42`/`"x"`)면 d.get 이 AttributeError(미캐치 튜플)로 빠져나가 reverify
        # scan 이 영구 skip. None 반환 → scan 재실행 → _write_sidecar 자가복구.
        if not isinstance(d, dict):
            return None
        return float(d.get("last_scan_epoch"))
    except (OSError, ValueError, TypeError):
        return None


def _write_sidecar() -> None:
    sc = _sidecar_path()
    try:
        sc.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _atomic_write(
        sc,
        json.dumps(
            {"last_scan_epoch": time.time(), "last_scan": time.strftime("%Y-%m-%dT%H:%M:%S")}
        ),
    )


def maybe_scan_due(mem_dir: Path, interval_days: int = REVERIFY_INTERVAL_DAYS) -> Optional[dict]:
    """sidecar last_scan 이 interval 보다 오래됐(또는 부재)으면 scan, 아니면 None.

    SessionEnd best-effort 트리거용 — 사실상 주 1회.
    """
    last = _read_sidecar_last_scan()
    # 0 <= delta: 미래 timestamp(클록 역행/NTP 후진 보정)면 delta<0 → 통과(scan)해서
    # sidecar 를 정상 time.time() 으로 self-heal. 가드 없으면 미래 last_scan 이
    # 영구 SKIP 을 유발해 reverify 가 wall-clock 따라잡을 때까지 silent 사망(audit R3).
    now = time.time()
    if last is not None and 0 <= (now - last) < interval_days * 86400:
        return None
    # bug-audit 2026-06-02 (#26): 동시 SessionEnd(sibling Conductor workspaces)가
    # 같은 stale sidecar 를 읽고 모두 due 판정 → 전체 메모리 재검증 스캔이 프로세스
    # 수만큼 중복 실행(데이터 손상은 아니나 CPU/IO + os.replace churn 낭비). flock
    # (LOCK_NB)로 한 프로세스만 스캔하도록 직렬화 — memory_indexer 의 동일 패턴.
    # 락 실패 = 다른 프로세스가 이미 스캔 중 → 즉시 skip(None). 락 획득 후 sidecar
    # 재확인으로, 막 끝난 직후 진입한 프로세스는 중복 스캔 대신 skip.
    import errno  # noqa: WPS433
    import fcntl  # noqa: WPS433  POSIX (macOS)
    lock_path = _sidecar_path().with_suffix(".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        # 락 파일조차 못 열면 직렬화는 포기하고 단독 진행(기존 동작 보존).
        return scan_memories(mem_dir)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            # codex R2: contention(EAGAIN/EWOULDBLOCK/EACCES)만 양보(skip). flock
            # 미지원 FS(ENOLCK/EOPNOTSUPP/EINVAL 등)는 직렬화 포기하고 단독 스캔 —
            # 그렇지 않으면 그런 FS 에서 reverify 가 영구히 스캔 안 함.
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                return None  # 다른 프로세스가 스캔 중 — 양보
            return scan_memories(mem_dir)
        last2 = _read_sidecar_last_scan()
        now2 = time.time()
        if last2 is not None and 0 <= (now2 - last2) < interval_days * 86400:
            return None  # 락 대기 사이 다른 프로세스가 막 스캔 완료
        return scan_memories(mem_dir)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass
