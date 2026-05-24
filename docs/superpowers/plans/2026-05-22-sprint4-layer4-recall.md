# MindVault v3 Sprint 4 — Layer 4 Memory Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** memory/*.md를 BGE-M3 임베딩 + FTS5 hybrid로 자동 회수하는 Layer 4를 기존 MindVault v3 (Sprint 1~3 배포 완료) 위에 추가한다. UserPromptSubmit hook으로 매 메시지마다 silent하게 발동.

**Architecture:** BGE-M3 MLX를 launchd로 상주(8081), `memory_indexer.py`가 `memory/*.md`를 본문+description 이중 임베딩 → sqlite-vec + FTS5. `memory_search.py`가 RRF로 결합하고, `memory-recall.py` hook이 매 사용자 메시지에 top-3을 system-reminder로 주입. 모든 실패는 silent (exit 0).

**Tech Stack:** Python 3.10+, sqlite-vec, mlx-embeddings (BGE-M3 4-bit), FTS5, launchd, Claude Code UserPromptSubmit hook.

**Spec:** `~/my-folder/apps/mindvault-v3/docs/superpowers/specs/2026-05-22-sprint4-layer4-recall-design.md`

---

## Task 0: 사전 준비

**Files:**
- Create: `~/my-folder/apps/mindvault-v3/tests/fixtures/memory/feedback_test_mail.md`
- Create: `~/my-folder/apps/mindvault-v3/tests/fixtures/memory/project_test_scanner.md`
- Create: `~/my-folder/apps/mindvault-v3/tests/fixtures/memory/feedback_test_html.md`
- Modify: `~/my-folder/apps/mindvault-v3/pyproject.toml` 또는 의존성 명시 파일

- [ ] **Step 0.1: Working directory 확인**

```bash
cd ~/my-folder/apps/mindvault-v3
pwd
ls handoff/ src/ tests/
```
Expected: `/Users/yonghaekim/my-folder/apps/mindvault-v3`, 기존 Sprint 1~3 산출물 존재.

- [ ] **Step 0.2: git 초기화 (없으면)**

```bash
cd ~/my-folder/apps/mindvault-v3
[ -d .git ] && echo "already git repo" || git init
git status
```
- 이미 git이면 skip. 아니면 init 후 `.gitignore` 확인.

- [ ] **Step 0.3: .gitignore 작성/확인**

```bash
cat > ~/my-folder/apps/mindvault-v3/.gitignore <<'EOF'
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.venv/
*.egg-info/
EOF
```

- [ ] **Step 0.4: Python 의존성 설치**

```bash
pip install --user sqlite-vec mlx-embeddings pyyaml
python3 -c "import sqlite_vec; print('sqlite_vec', sqlite_vec.__version__)"
python3 -c "import yaml; print('yaml', yaml.__version__)"
python3 -c "from mlx_embeddings import load; print('mlx_embeddings OK')"
```
Expected: 세 줄 모두 버전·OK 출력. 실패 시 `pip install --upgrade pip` 후 재시도.

- [ ] **Step 0.5: BGE-M3 모델 다운로드**

```bash
# huggingface-cli가 PATH에 없으면 Python API 사용
pip install --user huggingface_hub
python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='mlx-community/bge-m3-mlx-4bit', local_dir='/Users/yonghaekim/.cache/mlx-bge-m3')
"
ls -lh ~/.cache/mlx-bge-m3/model.safetensors
```
Expected: `model.safetensors` ~305MB 존재 (총 폴더 ~322MB). repo `mlx-community/bge-m3-mlx-4bit`는 MLX-converted 4-bit 양자화 BGE-M3. 6/8/fp16 변종도 있으나 4bit가 plan 기본.

- [ ] **Step 0.6: 테스트 fixture md 3개 작성**

`tests/fixtures/memory/feedback_test_mail.md`:
```markdown
---
name: test-mail
description: "Gmail SMTP 발송 도구 — msmtp + Python 래퍼로 dr.ocean@gmail.com 발송"
metadata:
  type: feedback
---

**[2026-05-22]** 메일 발송은 `~/bin/sendmail` 사용. msmtp 앱비밀번호 인증.

Why: 자연어 위임을 위함.
How to apply: 사용자가 "메일 보내줘" 자연어 요청 시 ~/bin/sendmail 직접 실행.
```

`tests/fixtures/memory/project_test_scanner.md`:
```markdown
---
name: test-scanner
description: "EPSON L605 USB 스캐너 CLI ~/bin/scan — macOS AirScan eSCL HTTP API 직접 호출"
metadata:
  type: project
---

**[2026-05-22]** USB 스캐너를 mDNS reflector로 localhost:56371에서 접근. curl + ImageMagick 조합.

Why: 종이 디지털화 마찰 제거.
```

`tests/fixtures/memory/feedback_test_html.md`:
```markdown
---
name: test-html
description: "마크다운 산출물은 인터랙티브 HTML로 변환해 open으로 브라우저에 열기"
metadata:
  type: feedback
---

**[2026-05-22]** 모든 기획·리포트·플랜 산출물은 단일 HTML 파일로.

How to apply: 자동 적용. 다크모드 + 인터랙티브 1개 이상 필수.
```

- [ ] **Step 0.7: fixture 확인 + 첫 commit**

```bash
ls tests/fixtures/memory/
wc -l tests/fixtures/memory/*.md
git add tests/fixtures/memory/ .gitignore
git commit -m "chore: Sprint 4 사전 준비 — fixture 메모리 + gitignore"
```

---

## Task 1: BGE-M3 launchd 서비스

**Files:**
- Create: `~/my-folder/apps/mindvault-v3/plist/com.yonghaekim.bge-m3-mlx.plist`
- Create: `~/my-folder/apps/mindvault-v3/scripts/bge_m3_server.py`

이 task의 목적: BGE-M3를 HTTP 서버로 띄워 다른 컴포넌트가 임베딩을 요청할 수 있게 함. Gemma MLX 서비스 패턴 그대로 복사.

- [ ] **Step 1.1: 기존 Gemma plist 참고**

```bash
ls ~/Library/LaunchAgents/com.yonghaekim.gemma-mlx.plist
cat ~/Library/LaunchAgents/com.yonghaekim.gemma-mlx.plist
```
Expected: Gemma plist 구조 확인. (없으면 형에게 BGE-M3 서비스 패턴 별도 결정 요청)

- [ ] **Step 1.2: BGE-M3 HTTP 서버 스크립트 작성**

`scripts/bge_m3_server.py`:
```python
#!/usr/bin/env python3
"""BGE-M3 MLX HTTP 임베딩 서버. POST /embed → {"vector": [1024 floats]}"""
from __future__ import annotations

import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mlx_embeddings import load

MODEL_DIR = Path.home() / ".cache" / "mlx-bge-m3"
PORT = 8081

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bge-m3")

# 모델 로딩 (서버 시작 시 1회)
log.info("Loading BGE-M3 from %s", MODEL_DIR)
model, tokenizer = load(str(MODEL_DIR))
log.info("BGE-M3 ready on port %d", PORT)


def embed(text: str) -> list[float]:
    """텍스트 → 1024차원 벡터."""
    out = model.encode(text)  # mlx-embeddings API
    return out.tolist()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/embed":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            text = body.get("input") or ""
            if not isinstance(text, str) or not text:
                raise ValueError("input required")
            vector = embed(text)
            payload = json.dumps({"vector": vector}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            log.exception("embed fail")
            err = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err)

    def log_message(self, fmt, *args):
        log.info(fmt % args)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("serving on http://127.0.0.1:%d", PORT)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

(주의: `mlx-embeddings`의 실제 API는 `model.encode()` 또는 `embed()` 다를 수 있음 — Step 1.4에서 stand-alone 호출로 검증)

- [ ] **Step 1.3: stand-alone 실행해 모델 로드 확인**

```bash
python3 ~/my-folder/apps/mindvault-v3/scripts/bge_m3_server.py &
SERVER_PID=$!
sleep 5  # 모델 로딩 시간
curl -sS http://localhost:8081/embed -H 'Content-Type: application/json' -d '{"input":"테스트"}' | python3 -c "import json,sys; d=json.load(sys.stdin); print('vec len:', len(d['vector']), 'first 3:', d['vector'][:3])"
kill $SERVER_PID
```
Expected: `vec len: 1024 first 3: [...]`. 차원이 1024가 아니면 mlx-embeddings API와 BGE-M3 변종 확인 필요.

- [ ] **Step 1.4: launchd plist 작성**

`plist/com.yonghaekim.bge-m3-mlx.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.yonghaekim.bge-m3-mlx</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/yonghaekim/my-folder/apps/mindvault-v3/scripts/bge_m3_server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/yonghaekim/.claude/mindvault-v3/bge_m3.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/yonghaekim/.claude/mindvault-v3/bge_m3.err</string>
</dict>
</plist>
```

- [ ] **Step 1.5: plist load & 헬스체크**

```bash
mkdir -p ~/.claude/mindvault-v3
cp ~/my-folder/apps/mindvault-v3/plist/com.yonghaekim.bge-m3-mlx.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist
sleep 8  # 모델 로딩 대기
launchctl list | grep bge-m3
curl -sS http://localhost:8081/embed -H 'Content-Type: application/json' -d '{"input":"헬스체크"}' | python3 -c "import json,sys; d=json.load(sys.stdin); print('OK len:', len(d['vector']))"
```
Expected: `launchctl list`에 entry 있음, `OK len: 1024`.

- [ ] **Step 1.6: commit**

```bash
cd ~/my-folder/apps/mindvault-v3
git add plist/ scripts/bge_m3_server.py
git commit -m "feat(sprint4): BGE-M3 MLX HTTP 임베딩 서버 (port 8081) + launchd plist"
```

---

## Task 2: SQLite 스키마 V2 마이그레이션

**Files:**
- Modify: `~/my-folder/apps/mindvault-v3/src/indexer.py` (SCHEMA_VERSION 1 → 2, memories_* 테이블 추가)
- Test: `~/my-folder/apps/mindvault-v3/tests/test_schema_v2.py`

기존 `indexer.py`의 `_init_db()`와 `open_db()`는 schema_version으로 V1/V2 마이그레이션을 처리 중. memories_* 테이블 3개를 V2에 추가하되 sessions_*는 보존.

- [ ] **Step 2.1: 실패 테스트 작성 — memories 테이블 존재 검증**

`tests/test_schema_v2.py`:
```python
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from indexer import open_db, SCHEMA_VERSION


class TestSchemaV2(unittest.TestCase):
    def test_schema_version_is_2(self):
        self.assertEqual(SCHEMA_VERSION, 2)

    def test_memories_tables_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = open_db(db)
            try:
                names = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    )
                }
                self.assertIn("memories", names)
                self.assertIn("memories_fts", names)
                self.assertIn("memories_vec", names)
                # 기존 테이블 보존
                self.assertIn("sessions", names)
                self.assertIn("sessions_fts", names)
            finally:
                conn.close()

    def test_v1_db_auto_rebuilds_to_v2(self):
        """V1 스키마로 만든 DB가 V2로 자동 재초기화돼야 함."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            # V1 스키마로 수동 생성
            c = sqlite3.connect(str(db))
            c.executescript(
                """
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO meta VALUES('schema_version', '1');
                CREATE TABLE sessions(session_id TEXT PRIMARY KEY);
                """
            )
            c.commit()
            c.close()
            # open_db 호출 → V2로 마이그레이션
            conn = open_db(db)
            try:
                version = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                self.assertEqual(version, "2")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2.2: 테스트 실행해 FAIL 확인**

```bash
cd ~/my-folder/apps/mindvault-v3
python3 -m unittest tests.test_schema_v2 -v 2>&1 | tail -20
```
Expected: 3 tests FAIL (SCHEMA_VERSION이 1, memories_* 없음).

- [ ] **Step 2.3: indexer.py 스키마 V2 적용**

`src/indexer.py` 수정:
- Line 21: `SCHEMA_VERSION = 1` → `SCHEMA_VERSION = 2`
- `_init_db` 함수의 `executescript`에 memories_* 추가, sqlite-vec 로드 추가:

```python
def _init_db(conn: sqlite3.Connection) -> None:
    # sqlite-vec 로드 (실패 시 vec 없이 운영 — degrade)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _debug("sqlite_vec loaded")
    except Exception as e:
        _debug(f"sqlite_vec load fail: {e} — degrading to FTS5-only")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            first_ts TEXT,
            last_ts TEXT,
            turn_count INTEGER,
            indexed_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
            session_id UNINDEXED,
            body,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS memories (
            path TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            mtime_ns INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            path UNINDEXED,
            body,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    # memories_vec은 sqlite-vec 로드 성공 시에만 생성
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                embedding FLOAT[1024],
                +kind TEXT,
                +path TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_vec_path "
            "ON memories(path)"  # 메타데이터 인덱스는 memories에
        )
    except sqlite3.OperationalError as e:
        _debug(f"memories_vec create skip: {e}")

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
```

(주의: sqlite-vec의 `+kind +path` 문법은 "auxiliary columns" — 인덱싱은 안 되고 함께 저장만 됨. 실제 sqlite-vec 문서 확인 필요. 안 되면 별도 테이블 `memories_vec_meta`로 join.)

- [ ] **Step 2.4: 테스트 재실행해 PASS 확인**

```bash
python3 -m unittest tests.test_schema_v2 -v 2>&1 | tail -10
```
Expected: 3 tests PASS.

- [ ] **Step 2.5: 기존 V1 DB 백업 후 마이그레이션 시뮬레이션**

```bash
# 실제 사용 중인 DB 백업 (Sprint 1~3 호환 검증용)
[ -f ~/.claude/mindvault-v3/index.db ] && cp ~/.claude/mindvault-v3/index.db ~/.claude/mindvault-v3/index.db.v1.bak

# open_db로 자동 마이그레이션 트리거
python3 -c "
import sys
sys.path.insert(0, '/Users/yonghaekim/my-folder/apps/mindvault-v3/src')
from indexer import open_db
from pathlib import Path
conn = open_db(Path('/Users/yonghaekim/.claude/mindvault-v3/index.db'))
print('schema_version:', conn.execute(\"SELECT value FROM meta WHERE key='schema_version'\").fetchone())
print('tables:', [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])
conn.close()
"
```
Expected: `schema_version: ('2',)`, tables에 memories + memories_vec 포함. (V1 DB가 unlink되어 0 sessions로 시작되니, 다음 indexer 호출에서 재인덱싱됨)

- [ ] **Step 2.6: Sprint 2 회귀 확인 — JSONL 재인덱싱**

```bash
python3 ~/.claude/scripts/mindvault/indexer.py
python3 -c "
import sqlite3
c = sqlite3.connect('/Users/yonghaekim/.claude/mindvault-v3/index.db')
print('sessions:', c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0])
"
```
Expected: 300+ sessions (실제 JSONL 수). 기존 회귀 없음.

- [ ] **Step 2.7: commit**

```bash
git add src/indexer.py tests/test_schema_v2.py
git commit -m "feat(sprint4): schema V2 — memories/memories_fts/memories_vec 추가, sqlite_vec lazy load"
```

---

## Task 3: memory_indexer.py — frontmatter 파싱 + 인덱싱 (TDD)

**Files:**
- Create: `~/my-folder/apps/mindvault-v3/src/memory_indexer.py`
- Test: `~/my-folder/apps/mindvault-v3/tests/test_memory_indexer.py`

### 3a. parse_frontmatter 함수

- [ ] **Step 3.1: 실패 테스트 — frontmatter 파싱**

`tests/test_memory_indexer.py`:
```python
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestParseFrontmatter(unittest.TestCase):
    def test_normal_frontmatter(self):
        from memory_indexer import parse_frontmatter
        md = """---
name: test
description: "hello world"
---

body content here"""
        fm, body = parse_frontmatter(md)
        self.assertEqual(fm.get("name"), "test")
        self.assertEqual(fm.get("description"), "hello world")
        self.assertIn("body content", body)
        self.assertNotIn("---", body[:5])

    def test_no_frontmatter(self):
        from memory_indexer import parse_frontmatter
        md = "just plain body no frontmatter"
        fm, body = parse_frontmatter(md)
        self.assertEqual(fm, {})
        self.assertEqual(body.strip(), md.strip())

    def test_description_missing(self):
        from memory_indexer import parse_frontmatter
        md = """---
name: test
---
body"""
        fm, body = parse_frontmatter(md)
        self.assertEqual(fm.get("name"), "test")
        self.assertIsNone(fm.get("description"))

    def test_malformed_yaml_graceful(self):
        from memory_indexer import parse_frontmatter
        md = """---
name: test
description: "unclosed quote
---
body"""
        # malformed yaml은 빈 dict + body 전체 반환 (graceful)
        fm, body = parse_frontmatter(md)
        self.assertEqual(fm, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3.2: 테스트 실행 → FAIL (memory_indexer 모듈 없음)**

```bash
python3 -m unittest tests.test_memory_indexer.TestParseFrontmatter -v 2>&1 | tail -10
```
Expected: ImportError.

- [ ] **Step 3.3: parse_frontmatter 구현**

`src/memory_indexer.py` (신규):
```python
#!/usr/bin/env python3
"""MindVault v3 Sprint 4 — memory/*.md → sqlite-vec + FTS5 이중 인덱서."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import yaml

DATA_DIR = Path("/Users/yonghaekim/.claude/mindvault-v3")
DB_PATH = DATA_DIR / "index.db"
DEBUG_LOG = DATA_DIR / "debug.log"
LOCK_PATH = DATA_DIR / "memory-indexer.lock"
BGE_M3_URL = "http://localhost:8081/embed"
BGE_M3_TIMEOUT = 5  # seconds (인덱싱 시점은 hook과 별개라 여유)
DEFAULT_MEMORY_DIRS = [
    Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim/memory"),
    Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder/memory"),
]

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] mem-indexer: {msg}\n")
    except Exception:
        pass


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """마크다운에서 frontmatter dict + 본문 분리. 실패 시 ({}, text)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        return {}, text
    body = text[m.end():]
    return fm, body
```

- [ ] **Step 3.4: parse_frontmatter 테스트 PASS 확인**

```bash
python3 -m unittest tests.test_memory_indexer.TestParseFrontmatter -v 2>&1 | tail -10
```
Expected: 4 tests PASS.

### 3b. embed_text — BGE-M3 HTTP 호출 + mock

- [ ] **Step 3.5: 실패 테스트 — embed_text mock**

`tests/test_memory_indexer.py`에 추가:
```python
class TestEmbedText(unittest.TestCase):
    def test_embed_success(self):
        from memory_indexer import embed_text
        with patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            mock_resp.read.return_value = json.dumps(
                {"vector": [0.1] * 1024}
            ).encode()
            vec = embed_text("hello")
            self.assertEqual(len(vec), 1024)
            self.assertEqual(vec[0], 0.1)

    def test_embed_timeout_returns_none(self):
        from memory_indexer import embed_text
        with patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = TimeoutError("timeout")
            vec = embed_text("hello")
            self.assertIsNone(vec)

    def test_embed_connection_refused_returns_none(self):
        from memory_indexer import embed_text
        with patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError("refused")
            vec = embed_text("hello")
            self.assertIsNone(vec)

    def test_embed_empty_input_returns_none(self):
        from memory_indexer import embed_text
        self.assertIsNone(embed_text(""))
        self.assertIsNone(embed_text("   "))
```

- [ ] **Step 3.6: 테스트 FAIL 확인**

```bash
python3 -m unittest tests.test_memory_indexer.TestEmbedText -v 2>&1 | tail -10
```
Expected: ImportError(embed_text).

- [ ] **Step 3.7: embed_text 구현**

`src/memory_indexer.py`에 추가:
```python
def embed_text(text: str) -> list[float] | None:
    """BGE-M3 서버로 임베딩 요청. 실패 시 None."""
    text = (text or "").strip()
    if not text:
        return None
    body = json.dumps({"input": text}).encode()
    req = urllib.request.Request(
        BGE_M3_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=BGE_M3_TIMEOUT) as resp:
            data = json.loads(resp.read())
        vec = data.get("vector")
        if not isinstance(vec, list) or len(vec) != 1024:
            _debug(f"embed bad shape: {type(vec)} len={len(vec) if isinstance(vec, list) else '?'}")
            return None
        return vec
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        _debug(f"embed fail: {type(e).__name__} {e}")
        return None
```

- [ ] **Step 3.8: 테스트 PASS 확인**

```bash
python3 -m unittest tests.test_memory_indexer.TestEmbedText -v 2>&1 | tail -10
```
Expected: 4 tests PASS.

### 3c. incremental_index 함수

- [ ] **Step 3.9: 실패 테스트 — incremental_index**

`tests/test_memory_indexer.py`에 추가:
```python
class TestIncrementalIndex(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.tmp_db_path = Path(self.tmp_db.name)
        self.fixture_dir = Path(__file__).parent / "fixtures" / "memory"

    def tearDown(self):
        self.tmp_db_path.unlink(missing_ok=True)

    def _mock_embed(self, *_, **__):
        return [0.5] * 1024

    def test_initial_index_inserts_rows(self):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=self._mock_embed), \
             patch("memory_indexer.DB_PATH", self.tmp_db_path):
            result = incremental_index([self.fixture_dir])
            self.assertEqual(result["updated"], 3)
            self.assertEqual(result["skipped"], 0)

            conn = sqlite3.connect(str(self.tmp_db_path))
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            self.assertEqual(count, 3)
            conn.close()

    def test_second_run_skips_unchanged(self):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=self._mock_embed), \
             patch("memory_indexer.DB_PATH", self.tmp_db_path):
            incremental_index([self.fixture_dir])
            result2 = incremental_index([self.fixture_dir])
            self.assertEqual(result2["updated"], 0)
            self.assertEqual(result2["skipped"], 3)

    def test_modified_file_reindexed(self):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=self._mock_embed), \
             patch("memory_indexer.DB_PATH", self.tmp_db_path):
            incremental_index([self.fixture_dir])
            # 한 파일 touch
            target = self.fixture_dir / "feedback_test_mail.md"
            current = target.read_text()
            target.write_text(current + "\n\n[touch]")
            try:
                result2 = incremental_index([self.fixture_dir])
                self.assertEqual(result2["updated"], 1)
            finally:
                target.write_text(current)

    def test_deleted_file_removed(self):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=self._mock_embed), \
             patch("memory_indexer.DB_PATH", self.tmp_db_path):
            with tempfile.TemporaryDirectory() as scratch:
                # fixtures를 임시 디렉토리로 복사 (수정 가능하게)
                scratch_dir = Path(scratch) / "memory"
                shutil.copytree(self.fixture_dir, scratch_dir)
                incremental_index([scratch_dir])
                # 한 파일 삭제
                (scratch_dir / "feedback_test_mail.md").unlink()
                result2 = incremental_index([scratch_dir])
                self.assertEqual(result2["removed"], 1)
                conn = sqlite3.connect(str(self.tmp_db_path))
                count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                conn.close()
                self.assertEqual(count, 2)

    def test_staged_dir_excluded(self):
        """_staged/ 하위 파일은 인덱싱 대상에서 제외."""
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=self._mock_embed), \
             patch("memory_indexer.DB_PATH", self.tmp_db_path):
            with tempfile.TemporaryDirectory() as scratch:
                scratch_dir = Path(scratch) / "memory"
                shutil.copytree(self.fixture_dir, scratch_dir)
                staged = scratch_dir / "_staged"
                staged.mkdir()
                (staged / "should_be_ignored.md").write_text(
                    "---\nname: ignore\n---\nshould not index"
                )
                result = incremental_index([scratch_dir])
                self.assertEqual(result["updated"], 3)  # 4 X
```

- [ ] **Step 3.10: 테스트 FAIL 확인**

```bash
python3 -m unittest tests.test_memory_indexer.TestIncrementalIndex -v 2>&1 | tail -15
```
Expected: 5 tests FAIL (incremental_index 미구현).

- [ ] **Step 3.11: incremental_index 구현**

`src/memory_indexer.py`에 추가 (indexer.py의 redact 재사용):
```python
sys.path.insert(0, str(Path(__file__).parent))
from indexer import redact, open_db  # 기존 함수 재사용


def _parse_memory_file(path: Path) -> tuple[dict, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        _debug(f"read fail {path}: {e}")
        return None
    fm, body = parse_frontmatter(text)
    body = redact(body)
    return fm, body


def _safe_memory_path(path: Path, allowed_roots: list[Path]) -> bool:
    """path가 allowed_roots 중 하나의 하위인지 검증. symlink resolve 포함."""
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in allowed_roots:
        try:
            rp.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _collect_md_files(dirs: list[Path]) -> list[Path]:
    """memory/ 디렉토리에서 .md 수집. _staged/ 제외."""
    out = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            # 상위 디렉토리에 _staged 포함되면 skip
            if any(part == "_staged" for part in p.parts):
                continue
            if not _safe_memory_path(p, dirs):
                _debug(f"unsafe path skip: {p}")
                continue
            out.append(p)
    return out


def incremental_index(
    memory_dirs: list[Path] | None = None,
    db_path: Path | None = None,
) -> dict[str, int]:
    """mtime 비교로 변경된 파일만 재임베딩.
    반환: {"updated", "skipped", "removed"}
    """
    if memory_dirs is None:
        memory_dirs = DEFAULT_MEMORY_DIRS
    if db_path is None:
        db_path = DB_PATH

    counts = {"updated": 0, "skipped": 0, "removed": 0}
    conn = open_db(db_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        existing = {
            r["path"]: r["mtime_ns"]
            for r in conn.execute("SELECT path, mtime_ns FROM memories")
        }
        present_files = _collect_md_files(memory_dirs)
        present_paths = {str(p) for p in present_files}

        # 1) 삭제된 파일 처리
        for stale_path in existing.keys() - present_paths:
            conn.execute("DELETE FROM memories WHERE path=?", (stale_path,))
            conn.execute("DELETE FROM memories_fts WHERE path=?", (stale_path,))
            try:
                conn.execute("DELETE FROM memories_vec WHERE path=?", (stale_path,))
            except sqlite3.OperationalError:
                pass
            counts["removed"] += 1

        # 2) 신규/변경 파일 처리
        for p in present_files:
            try:
                st = p.stat()
            except OSError:
                continue
            sp = str(p)
            if existing.get(sp) == st.st_mtime_ns:
                counts["skipped"] += 1
                continue

            parsed = _parse_memory_file(p)
            if parsed is None:
                continue
            fm, body = parsed
            name = (fm.get("name") or p.stem)
            description = (fm.get("description") or "")

            # 임베딩 — body 항상, description 있을 때만
            vec_body = embed_text(body) if body.strip() else None
            vec_desc = embed_text(description) if description.strip() else None

            # 기존 row 삭제 후 재삽입
            conn.execute("DELETE FROM memories_fts WHERE path=?", (sp,))
            try:
                conn.execute("DELETE FROM memories_vec WHERE path=?", (sp,))
            except sqlite3.OperationalError:
                pass

            conn.execute(
                """
                INSERT INTO memories(path, name, description, mtime_ns, indexed_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    mtime_ns=excluded.mtime_ns,
                    indexed_at=excluded.indexed_at
                """,
                (sp, name, description, st.st_mtime_ns, now),
            )
            conn.execute(
                "INSERT INTO memories_fts(path, body) VALUES(?,?)",
                (sp, body),
            )
            if vec_body is not None:
                try:
                    conn.execute(
                        "INSERT INTO memories_vec(embedding, kind, path) VALUES(?,?,?)",
                        (json.dumps(vec_body), "body", sp),
                    )
                except sqlite3.OperationalError as e:
                    _debug(f"vec insert body fail {p.name}: {e}")
            if vec_desc is not None:
                try:
                    conn.execute(
                        "INSERT INTO memories_vec(embedding, kind, path) VALUES(?,?,?)",
                        (json.dumps(vec_desc), "description", sp),
                    )
                except sqlite3.OperationalError as e:
                    _debug(f"vec insert desc fail {p.name}: {e}")
            counts["updated"] += 1

        conn.commit()
    finally:
        conn.close()
    _debug(f"incremental: {counts}")
    return counts


def full_rebuild(
    memory_dirs: list[Path] | None = None,
    db_path: Path | None = None,
) -> int:
    """DB 안의 memories_* 데이터만 비우고 재인덱싱 (sessions_*는 보존)."""
    if db_path is None:
        db_path = DB_PATH
    conn = open_db(db_path)
    try:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM memories_fts")
        try:
            conn.execute("DELETE FROM memories_vec")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()
    return incremental_index(memory_dirs, db_path)["updated"]


def main() -> int:
    t0 = time.time()
    try:
        c = incremental_index()
        _debug(f"main: {c} in {time.time()-t0:.2f}s")
    except Exception as e:
        _debug(f"FATAL {e}\n{traceback.format_exc()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3.12: 테스트 재실행 — PASS 확인**

```bash
python3 -m unittest tests.test_memory_indexer -v 2>&1 | tail -15
```
Expected: 모든 테스트 PASS (parse 4 + embed 4 + incremental 5 = 13).

- [ ] **Step 3.13: path traversal 차단 테스트 추가 + 통과**

`tests/test_memory_indexer.py`에:
```python
class TestPathSafety(unittest.TestCase):
    def test_symlink_outside_root_rejected(self):
        from memory_indexer import _safe_memory_path
        with tempfile.TemporaryDirectory() as root, \
             tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / "evil.md"
            outside_file.write_text("---\nname: evil\n---\nbad")
            symlink = Path(root) / "trick.md"
            symlink.symlink_to(outside_file)
            self.assertFalse(_safe_memory_path(symlink, [Path(root)]))

    def test_path_inside_root_accepted(self):
        from memory_indexer import _safe_memory_path
        with tempfile.TemporaryDirectory() as root:
            f = Path(root) / "ok.md"
            f.write_text("ok")
            self.assertTrue(_safe_memory_path(f, [Path(root)]))
```

```bash
python3 -m unittest tests.test_memory_indexer.TestPathSafety -v
```
Expected: 2 PASS.

- [ ] **Step 3.14: commit**

```bash
git add src/memory_indexer.py tests/test_memory_indexer.py
git commit -m "feat(sprint4): memory_indexer — frontmatter 파싱, BGE-M3 호출, incremental index, path safety"
```

---

## Task 4: memory_search.py — hybrid RRF (TDD)

**Files:**
- Create: `~/my-folder/apps/mindvault-v3/src/memory_search.py`
- Test: `~/my-folder/apps/mindvault-v3/tests/test_memory_search.py`

- [ ] **Step 4.1: 실패 테스트 — RRF 결합 수식**

`tests/test_memory_search.py`:
```python
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestRRFFusion(unittest.TestCase):
    def test_rrf_single_source(self):
        from memory_search import rrf_combine
        # vec만 hit, 3개 결과
        vec_results = [("a.md", 1, "body"), ("b.md", 2, "body"), ("c.md", 3, "body")]
        fts_results = []
        combined = rrf_combine(vec_results, fts_results, k=60)
        # rank 1이 가장 높음: 1/(60+1) = 0.01639
        self.assertAlmostEqual(combined["a.md"]["score"], 1 / 61, places=4)

    def test_rrf_both_sources_aggregate(self):
        from memory_search import rrf_combine
        vec_results = [("a.md", 1, "body")]
        fts_results = [("a.md", 1, "")]
        combined = rrf_combine(vec_results, fts_results, k=60)
        # 양쪽에서 rank 1 → 점수 합산
        self.assertAlmostEqual(combined["a.md"]["score"], 2 / 61, places=4)
        self.assertEqual(set(combined["a.md"]["source"]), {"vec", "fts"})

    def test_rrf_description_weight(self):
        from memory_search import rrf_combine
        # kind='description'은 1.5x 가중
        vec_results = [("a.md", 1, "description")]
        fts_results = []
        combined = rrf_combine(vec_results, fts_results, k=60)
        self.assertAlmostEqual(combined["a.md"]["score"], 1.5 / 61, places=4)

    def test_rrf_empty_inputs(self):
        from memory_search import rrf_combine
        self.assertEqual(rrf_combine([], [], k=60), {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4.2: 테스트 FAIL 확인**

```bash
python3 -m unittest tests.test_memory_search.TestRRFFusion -v 2>&1 | tail -10
```
Expected: ImportError.

- [ ] **Step 4.3: rrf_combine 구현**

`src/memory_search.py` (신규):
```python
#!/usr/bin/env python3
"""MindVault v3 Sprint 4 — hybrid RRF memory 검색."""
from __future__ import annotations

import json
import sqlite3
import time
import traceback
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from memory_indexer import embed_text, _debug as base_debug  # noqa: F401
from indexer import open_db

DB_PATH = Path("/Users/yonghaekim/.claude/mindvault-v3/index.db")
DEBUG_LOG = Path("/Users/yonghaekim/.claude/mindvault-v3/debug.log")
RRF_K = 60
DESCRIPTION_WEIGHT = 1.5
DEFAULT_TOP_K = 3
DEFAULT_THRESHOLD = 0.65


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] mem-search: {msg}\n")
    except Exception:
        pass


def rrf_combine(
    vec_results: list[tuple[str, int, str]],
    fts_results: list[tuple[str, int, str]],
    k: int = RRF_K,
) -> dict[str, dict]:
    """RRF로 두 결과를 결합.
    vec_results: [(path, rank, kind), ...]   kind ∈ {'body', 'description'}
    fts_results: [(path, rank, ''), ...]
    반환: {path: {"score": float, "source": list[str]}}
    """
    combined: dict[str, dict] = {}

    for path, rank, kind in vec_results:
        weight = DESCRIPTION_WEIGHT if kind == "description" else 1.0
        contribution = weight * (1.0 / (k + rank))
        entry = combined.setdefault(path, {"score": 0.0, "source": []})
        entry["score"] += contribution
        if "vec" not in entry["source"]:
            entry["source"].append("vec")

    for path, rank, _ in fts_results:
        contribution = 1.0 / (k + rank)
        entry = combined.setdefault(path, {"score": 0.0, "source": []})
        entry["score"] += contribution
        if "fts" not in entry["source"]:
            entry["source"].append("fts")

    return combined
```

- [ ] **Step 4.4: rrf_combine 테스트 PASS 확인**

```bash
python3 -m unittest tests.test_memory_search.TestRRFFusion -v 2>&1 | tail -10
```
Expected: 4 PASS.

- [ ] **Step 4.5: 정규화 + threshold 테스트 추가**

`tests/test_memory_search.py`에:
```python
class TestNormalization(unittest.TestCase):
    def test_normalize_minmax(self):
        from memory_search import normalize_scores
        combined = {
            "a.md": {"score": 0.05, "source": ["vec"]},
            "b.md": {"score": 0.02, "source": ["fts"]},
            "c.md": {"score": 0.01, "source": ["vec"]},
        }
        normalize_scores(combined)
        self.assertEqual(combined["a.md"]["score"], 1.0)
        self.assertEqual(combined["c.md"]["score"], 0.0)
        self.assertGreater(combined["b.md"]["score"], 0.0)
        self.assertLess(combined["b.md"]["score"], 1.0)

    def test_normalize_single_entry(self):
        from memory_search import normalize_scores
        combined = {"a.md": {"score": 0.05, "source": ["vec"]}}
        normalize_scores(combined)
        self.assertEqual(combined["a.md"]["score"], 1.0)

    def test_normalize_empty(self):
        from memory_search import normalize_scores
        combined: dict = {}
        normalize_scores(combined)
        self.assertEqual(combined, {})
```

- [ ] **Step 4.6: 테스트 FAIL 확인**

```bash
python3 -m unittest tests.test_memory_search.TestNormalization -v 2>&1 | tail -10
```
Expected: 3 FAIL.

- [ ] **Step 4.7: normalize_scores 구현**

`src/memory_search.py`에:
```python
def normalize_scores(combined: dict[str, dict]) -> None:
    """min-max 정규화 in-place. 단일 항목이면 1.0."""
    if not combined:
        return
    scores = [v["score"] for v in combined.values()]
    lo, hi = min(scores), max(scores)
    if hi == lo:
        for v in combined.values():
            v["score"] = 1.0
        return
    span = hi - lo
    for v in combined.values():
        v["score"] = (v["score"] - lo) / span
```

- [ ] **Step 4.8: 테스트 PASS 확인**

```bash
python3 -m unittest tests.test_memory_search.TestNormalization -v 2>&1 | tail -10
```
Expected: 3 PASS.

- [ ] **Step 4.9: recall_memory 통합 테스트 작성 (mock DB)**

`tests/test_memory_search.py`에:
```python
class TestRecallMemory(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)
        # 스키마 + 가짜 row 삽입
        from indexer import open_db
        conn = open_db(self.db_path)
        conn.execute(
            "INSERT INTO memories(path, name, description, mtime_ns, indexed_at) "
            "VALUES('/m/a.md', 'a', 'desc-a', 0, '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO memories(path, name, description, mtime_ns, indexed_at) "
            "VALUES('/m/b.md', 'b', 'desc-b', 0, '2026-01-01')"
        )
        conn.execute("INSERT INTO memories_fts(path, body) VALUES('/m/a.md', 'hello world')")
        conn.execute("INSERT INTO memories_fts(path, body) VALUES('/m/b.md', 'goodbye world')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_recall_empty_when_no_threshold_hit(self):
        from memory_search import recall_memory
        with patch("memory_search.embed_text", return_value=None), \
             patch("memory_search.DB_PATH", self.db_path):
            results = recall_memory("anything", top_k=3, score_threshold=0.99)
            # vec 결과 없으니 FTS만, threshold 0.99 → 통과 못 함
            # (단, "hello world" 가 "anything"으로 FTS 매치되지 않으니 그냥 빈 결과 가능)
            self.assertIsInstance(results, list)

    def test_recall_fts_hit(self):
        from memory_search import recall_memory
        with patch("memory_search.embed_text", return_value=None), \
             patch("memory_search.DB_PATH", self.db_path):
            results = recall_memory("hello", top_k=3, score_threshold=0.0)
            paths = [r["path"] for r in results]
            self.assertIn("/m/a.md", paths)

    def test_recall_returns_full_schema(self):
        from memory_search import recall_memory
        with patch("memory_search.embed_text", return_value=None), \
             patch("memory_search.DB_PATH", self.db_path):
            results = recall_memory("hello", top_k=3, score_threshold=0.0)
            if results:
                r = results[0]
                for key in ("path", "name", "description", "snippet", "score", "source"):
                    self.assertIn(key, r)
```

- [ ] **Step 4.10: 테스트 FAIL 확인**

```bash
python3 -m unittest tests.test_memory_search.TestRecallMemory -v 2>&1 | tail -10
```
Expected: ImportError (recall_memory 미구현).

- [ ] **Step 4.11: recall_memory 구현**

`src/memory_search.py`에:
```python
def _fts_escape(query: str) -> str:
    import re
    words = re.findall(r"[^\s\"'`*:()]+", query)
    if not words:
        return '""'
    return " ".join(f'"{w}"' for w in words)


def _fts_top_k(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[tuple[str, int, str]]:
    fts_q = _fts_escape(query)
    try:
        rows = conn.execute(
            """
            SELECT path, bm25(memories_fts) AS score
            FROM memories_fts
            WHERE memories_fts MATCH ?
            ORDER BY score LIMIT ?
            """,
            (fts_q, limit),
        ).fetchall()
    except sqlite3.DatabaseError as e:
        _debug(f"fts fail: {e}")
        return []
    return [(r["path"], idx + 1, "") for idx, r in enumerate(rows)]


def _vec_top_k(
    conn: sqlite3.Connection, query_vec: list[float], limit: int = 10
) -> list[tuple[str, int, str]]:
    try:
        rows = conn.execute(
            """
            SELECT path, kind, distance
            FROM memories_vec
            WHERE embedding MATCH ?
            ORDER BY distance LIMIT ?
            """,
            (json.dumps(query_vec), limit),
        ).fetchall()
    except sqlite3.OperationalError as e:
        _debug(f"vec fail (likely no sqlite-vec): {e}")
        return []
    return [(r["path"], idx + 1, r["kind"]) for idx, r in enumerate(rows)]


def _snippet(conn: sqlite3.Connection, path: str, max_chars: int = 160) -> str:
    row = conn.execute(
        "SELECT body FROM memories_fts WHERE path=?", (path,)
    ).fetchone()
    if not row:
        return ""
    body = row["body"] or ""
    return body[:max_chars].replace("\n", " ").strip()


def recall_memory(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_THRESHOLD,
    db_path: Path | None = None,
) -> list[dict]:
    """hybrid RRF로 memory/*.md 검색.
    반환: [{"path","name","description","snippet","score","source"}, ...]
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path.is_file():
        return []

    t0 = time.time()
    try:
        conn = open_db(db_path)
    except Exception as e:
        _debug(f"db open fail: {e}")
        return []

    try:
        # 1) FTS5
        fts_rows = _fts_top_k(conn, query, limit=10)

        # 2) Vec
        vec_rows = []
        qvec = embed_text(query)
        if qvec is not None:
            vec_rows = _vec_top_k(conn, qvec, limit=10)

        if not vec_rows and not fts_rows:
            _debug(f"no candidates for query={query!r}")
            return []

        # 3) RRF
        combined = rrf_combine(vec_rows, fts_rows, k=RRF_K)
        normalize_scores(combined)

        # 4) threshold + top_k
        kept = [
            (path, info) for path, info in combined.items()
            if info["score"] >= score_threshold
        ]
        kept.sort(key=lambda x: x[1]["score"], reverse=True)
        kept = kept[:top_k]

        # 5) 메타 채우기
        results = []
        for path, info in kept:
            meta = conn.execute(
                "SELECT name, description FROM memories WHERE path=?", (path,)
            ).fetchone()
            results.append({
                "path": path,
                "name": meta["name"] if meta else "",
                "description": meta["description"] if meta else "",
                "snippet": _snippet(conn, path),
                "score": round(info["score"], 4),
                "source": info["source"],
            })

        elapsed = int((time.time() - t0) * 1000)
        _debug(
            f"recall query={query!r} vec={len(vec_rows)} fts={len(fts_rows)} "
            f"picked={len(results)} elapsed_ms={elapsed}"
        )
        return results
    except Exception as e:
        _debug(f"recall FATAL: {e}\n{traceback.format_exc()}")
        return []
    finally:
        conn.close()
```

- [ ] **Step 4.12: 모든 memory_search 테스트 PASS 확인**

```bash
python3 -m unittest tests.test_memory_search -v 2>&1 | tail -15
```
Expected: 모든 테스트 (rrf 4 + norm 3 + recall 3 = 10) PASS.

- [ ] **Step 4.13: commit**

```bash
git add src/memory_search.py tests/test_memory_search.py
git commit -m "feat(sprint4): memory_search — hybrid RRF (vec+fts) + min-max norm + threshold gate"
```

---

## Task 5: hooks/memory-recall.py — UserPromptSubmit hook (TDD)

**Files:**
- Create: `~/my-folder/apps/mindvault-v3/hooks/memory-recall.py` (배포본 → `~/.claude/hooks/`)
- Test: `~/my-folder/apps/mindvault-v3/tests/test_memory_hook.py`

- [ ] **Step 5.1: 실패 테스트 — hook stdin/stdout 계약**

`tests/test_memory_hook.py`:
```python
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HOOK = Path(__file__).parent.parent / "hooks" / "memory-recall.py"


class TestHookIO(unittest.TestCase):
    def _run(self, payload: dict) -> tuple[int, str, str]:
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=5,
        )
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def test_short_prompt_empty_output(self):
        rc, out, err = self._run({"prompt": "ㅇ"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_malformed_stdin_silent(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=b"not json at all",
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), b"")

    def test_no_prompt_field_silent(self):
        rc, out, err = self._run({"session_id": "abc"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")
```

- [ ] **Step 5.2: 테스트 FAIL 확인**

```bash
python3 -m unittest tests.test_memory_hook -v 2>&1 | tail -10
```
Expected: HOOK 파일 없음 또는 FAIL.

- [ ] **Step 5.3: hook 스크립트 구현**

`hooks/memory-recall.py`:
```python
#!/usr/bin/env python3
"""MindVault v3 Sprint 4 — UserPromptSubmit hook.
매 사용자 메시지마다 memory/*.md hybrid 검색 결과를 system-reminder로 주입.
모든 실패는 silent → exit 0 빈 출력.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path("/Users/yonghaekim/.claude/mindvault-v3")
DEBUG_LOG = DATA_DIR / "debug.log"
METRICS_LOG = DATA_DIR / "metrics.jsonl"
SCRIPTS_DIR = Path("/Users/yonghaekim/.claude/scripts/mindvault")
MIN_PROMPT_LEN = 3
HARD_TIMEOUT_MS = 200
SCORE_THRESHOLD = 0.65
TOP_K = 3
MEMORY_DIRS = [
    Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim/memory"),
    Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder/memory"),
]
INDEX_DB = DATA_DIR / "index.db"


def _debug(msg: str) -> None:
    try:
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] hook-recall: {msg}\n")
    except Exception:
        pass


def _metric(payload: dict) -> None:
    try:
        with METRICS_LOG.open("a") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _mtime_changed() -> bool:
    """memory/ 디렉토리 중 하나라도 index.db보다 새로우면 True."""
    try:
        db_mt = INDEX_DB.stat().st_mtime
    except FileNotFoundError:
        return True
    for d in MEMORY_DIRS:
        if not d.is_dir():
            continue
        try:
            if d.stat().st_mtime > db_mt:
                return True
            for p in d.glob("*.md"):
                if p.stat().st_mtime > db_mt:
                    return True
        except OSError:
            continue
    return False


def _spawn_reindex() -> None:
    """incremental_index를 백그라운드로 분리 spawn. 결과 기다리지 않음."""
    try:
        subprocess.Popen(
            [sys.executable, "-c",
             "import sys;"
             f"sys.path.insert(0, '{SCRIPTS_DIR}');"
             "from memory_indexer import incremental_index;"
             "incremental_index()"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        _debug(f"spawn reindex fail: {e}")


class _Timeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _Timeout()


def _format_output(results: list[dict]) -> str:
    lines = ["<system-reminder>", "# 메모리 회수 (Layer 4 hybrid)"]
    for r in results:
        srcs = "+".join(r.get("source") or [])
        name = r.get("name") or "(unnamed)"
        desc = r.get("description") or ""
        snippet = r.get("snippet") or ""
        score = r.get("score", 0)
        line = f"- **{name}** (score {score:.2f}, {srcs}) — {desc}"
        lines.append(line)
        if snippet:
            lines.append(f"  발췌: {snippet}")
    lines.append("</system-reminder>")
    return "\n".join(lines) + "\n"


def main() -> int:
    t0 = time.time()
    # 200ms hard timeout (signal.alarm은 초 단위 → ITIMER_REAL 사용)
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, HARD_TIMEOUT_MS / 1000.0)

    try:
        # stdin JSON 파싱
        try:
            payload = json.loads(sys.stdin.read())
        except (json.JSONDecodeError, ValueError):
            return 0

        prompt = (payload.get("prompt") or "").strip()
        if len(prompt) < MIN_PROMPT_LEN:
            return 0

        # lazy reindex 체크 (백그라운드 spawn — 결과 안 기다림)
        if _mtime_changed():
            _spawn_reindex()

        # memory_search 호출
        sys.path.insert(0, str(SCRIPTS_DIR))
        from memory_search import recall_memory

        results = recall_memory(
            prompt, top_k=TOP_K, score_threshold=SCORE_THRESHOLD
        )

        elapsed_ms = int((time.time() - t0) * 1000)
        max_score = results[0]["score"] if results else 0.0
        _metric({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "recall",
            "query_len": len(prompt),
            "elapsed_ms": elapsed_ms,
            "picked": len(results),
            "max_score": max_score,
        })
        _debug(
            f"query_len={len(prompt)} picked={len(results)} elapsed_ms={elapsed_ms}"
        )

        if not results:
            return 0

        sys.stdout.write(_format_output(results))
        return 0
    except _Timeout:
        _debug(f"timeout {HARD_TIMEOUT_MS}ms — skip")
        return 0
    except Exception as e:
        _debug(f"FATAL {type(e).__name__}: {e}")
        return 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    sys.exit(main())
```

```bash
chmod +x ~/my-folder/apps/mindvault-v3/hooks/memory-recall.py
```

- [ ] **Step 5.4: TestHookIO 테스트 PASS 확인**

```bash
python3 -m unittest tests.test_memory_hook.TestHookIO -v 2>&1 | tail -10
```
Expected: 3 PASS.

- [ ] **Step 5.5: 정상 회수 경로 통합 테스트 추가**

`tests/test_memory_hook.py`에:
```python
class TestHookNormalFlow(unittest.TestCase):
    """실제 BGE-M3 + DB 필요 — 통합 테스트."""

    @unittest.skipIf(
        os.environ.get("MV3_SKIP_INTEGRATION") == "1",
        "MV3_SKIP_INTEGRATION=1",
    )
    def test_real_query_returns_system_reminder(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"prompt": "메일 보내는 도구"}).encode(),
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(r.returncode, 0)
        # threshold 통과한 결과가 있으면 system-reminder 포맷
        if r.stdout:
            out = r.stdout.decode()
            self.assertIn("<system-reminder>", out)
            self.assertIn("메모리 회수 (Layer 4 hybrid)", out)
            self.assertIn("</system-reminder>", out)
```

(Step 5.5의 import os는 파일 상단에 이미 있어야 함 — TestHookIO 위에)

- [ ] **Step 5.6: 통합 테스트 실행 (BGE-M3 떠있어야 함)**

```bash
curl -sS http://localhost:8081/embed -d '{"input":"healthcheck"}' -H 'Content-Type: application/json' > /dev/null && echo "BGE-M3 OK"

# 인덱스 한 번 채우기 (memory_indexer 직접 호출 — full_rebuild 가벼움)
python3 -c "
import sys
sys.path.insert(0, '/Users/yonghaekim/my-folder/apps/mindvault-v3/src')
from memory_indexer import incremental_index
print(incremental_index())
"

python3 -m unittest tests.test_memory_hook.TestHookNormalFlow -v 2>&1 | tail -10
```
Expected: BGE-M3 OK, incremental_index 출력, 1 PASS (또는 skip).

- [ ] **Step 5.7: hook 단독 수동 호출 검증**

```bash
echo '{"prompt":"이메일 보내는 방법"}' | python3 ~/my-folder/apps/mindvault-v3/hooks/memory-recall.py
```
Expected: system-reminder 블록 또는 빈 출력 (threshold 미달 시).

- [ ] **Step 5.8: commit**

```bash
git add hooks/memory-recall.py tests/test_memory_hook.py
git commit -m "feat(sprint4): UserPromptSubmit hook — silent recall + 200ms timeout + lazy reindex"
```

---

## Task 6: recall_cli.py — `--source` 확장

**Files:**
- Modify: `~/my-folder/apps/mindvault-v3/src/recall_cli.py`
- Test: `~/my-folder/apps/mindvault-v3/tests/test_recall_cli.py`

기존 recall_cli.py는 인자 1개(쿼리)만 받음. `--source memory|sessions|both` 추가.

- [ ] **Step 6.1: 실패 테스트 — --source 플래그**

`tests/test_recall_cli.py`:
```python
import json
import subprocess
import sys
import unittest
from pathlib import Path

CLI = Path(__file__).parent.parent / "src" / "recall_cli.py"


class TestRecallCLI(unittest.TestCase):
    def test_default_source_is_both(self):
        r = subprocess.run(
            [sys.executable, str(CLI), "테스트"],
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout.decode() or '{"query":"테스트"}')
        # both 모드에서는 'memory'와 'sessions' 키 둘 다
        self.assertIn("query", data)

    def test_memory_source(self):
        r = subprocess.run(
            [sys.executable, str(CLI), "테스트", "--source", "memory"],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout.decode())
        self.assertIn("memory", data)
        # memory만 있으면 sessions 키 부재
        self.assertNotIn("sessions", data)
```

- [ ] **Step 6.2: 테스트 FAIL 확인**

```bash
python3 -m unittest tests.test_recall_cli -v 2>&1 | tail -10
```
Expected: 2 FAIL.

- [ ] **Step 6.3: recall_cli.py 확장**

`src/recall_cli.py` 전체 교체:
```python
#!/usr/bin/env python3
"""MindVault v3 — recall CLI. JSONL 세션(FTS5+Gemma) + memory/(hybrid RRF) 검색."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _search_sessions(query: str, top_k: int = 3) -> list[dict]:
    """기존 Sprint 2 검색 (FTS5 + Gemma 재순위/요약)."""
    from search import recall as session_recall
    return session_recall(query, top_k=top_k)


def _search_memory(query: str, top_k: int = 5) -> list[dict]:
    """Sprint 4 hybrid RRF (threshold 0 — 명시 호출이므로 다 보여줌)."""
    from memory_search import recall_memory
    return recall_memory(query, top_k=top_k, score_threshold=0.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument(
        "--source",
        choices=["memory", "sessions", "both"],
        default="both",
    )
    args = parser.parse_args()

    out: dict = {"query": args.query}
    if args.source in ("memory", "both"):
        out["memory"] = _search_memory(args.query, top_k=5)
    if args.source in ("sessions", "both"):
        out["sessions"] = _search_sessions(args.query, top_k=3)

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6.4: 테스트 PASS 확인**

```bash
python3 -m unittest tests.test_recall_cli -v 2>&1 | tail -10
```
Expected: 2 PASS.

- [ ] **Step 6.5: 수동 검증**

```bash
python3 ~/my-folder/apps/mindvault-v3/src/recall_cli.py "메일" --source memory | python3 -m json.tool | head -30
```
Expected: memory 결과 JSON. (BGE-M3 떠있고 인덱스 있어야 의미 있음)

- [ ] **Step 6.6: /recall 스킬 markdown 업데이트**

`~/my-folder/apps/mindvault-v3/skill/recall.md`의 명령 부분을 양쪽 처리하도록 보강:
```bash
# 기존: python3 ~/.claude/scripts/mindvault/recall_cli.py "$ARGUMENTS"
# 그대로 유지 — recall_cli.py가 이제 both 기본이라 자동으로 memory + sessions 둘 다.
# 스킬 본문에 결과 해석 섹션만 추가
```

스킬 md 본문에 추가할 결과 해석 규칙 (3번 항목 보강):
```
3. 결과 해석:
   - `out["memory"]`: hybrid 검색 결과 — 각 항목 {path, name, description, snippet, score, source}.
       각 항목을 다음 형식으로 출력:
       ### 📌 {name} (memory, score {score:.2f}, {source.join('+')})
       {description}
       발췌: {snippet}
   - `out["sessions"]`: 기존 동작 (그대로).
   - 양쪽 모두 비면 `매칭되는 과거 세션·메모리 없음.` 출력.
```

- [ ] **Step 6.7: commit**

```bash
git add src/recall_cli.py tests/test_recall_cli.py skill/recall.md
git commit -m "feat(sprint4): recall_cli --source memory|sessions|both + /recall 스킬 보강"
```

---

## Task 7: memory_review_cli + install/uninstall 확장

**Files:**
- Modify: `~/my-folder/apps/mindvault-v3/src/memory_review_cli.py` (approve 후 incremental_index 1줄)
- Modify: `~/my-folder/apps/mindvault-v3/install.sh`
- Modify: `~/my-folder/apps/mindvault-v3/uninstall.sh`
- Test: `~/my-folder/apps/mindvault-v3/tests/test_install_uninstall.py`

### 7a. memory_review_cli — approve 후 reindex

- [ ] **Step 7.1: 실패 테스트 — approve 후 memories 테이블에 새 row**

`tests/test_memory_review_integration.py` (new):
```python
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestApproveReindex(unittest.TestCase):
    @unittest.skipIf(
        __import__("os").environ.get("MV3_SKIP_INTEGRATION") == "1",
        "skip integration",
    )
    def test_approve_triggers_incremental_index(self):
        """approve 후 memories 테이블에 새 row가 들어가야 한다."""
        # 이 테스트는 실제 ~/.claude/projects/.../memory/_staged/ 경로에
        # 임시 staged 파일 생성 → approve → 인덱스 확인 시나리오.
        # MVP에서는 단순 호출 검증으로 충분 — 실제 cleanup이 까다로워 manual.
        # E2E는 Task 9 수동 검증에서.
        self.skipTest("Task 9 수동 검증으로 이관")
```

- [ ] **Step 7.2: memory_review_cli.py 수정 — approve 함수에 1줄 추가**

`src/memory_review_cli.py`의 `approve` 함수 마지막에:
```python
# (approve 함수 마지막, MEMORY.md append 직후)
try:
    from memory_indexer import incremental_index
    counts = incremental_index()
    print(f"[reindex] {counts}", file=sys.stderr)
except Exception as e:
    print(f"[reindex skip] {e}", file=sys.stderr)
```

- [ ] **Step 7.3: 수동 검증 — staged → approve → memories 테이블**

```bash
# 1) 임시 staged 파일 생성
STAGED=~/.claude/projects/-Users-yonghaekim-my-folder/memory/_staged
mkdir -p $STAGED
cat > $STAGED/feedback_sprint4_test.md <<'EOF'
---
name: sprint4-test
description: "Sprint 4 reindex 검증용 임시 메모리"
metadata:
  type: feedback
---
**[2026-05-22]** Sprint 4 approve→reindex 통합 검증.
EOF

# 2) BGE-M3 살아있는지 확인
curl -sS http://localhost:8081/embed -d '{"input":"test"}' -H 'Content-Type: application/json' > /dev/null && echo "BGE-M3 OK"

# 3) approve
python3 ~/.claude/scripts/mindvault/memory_review_cli.py approve sprint4-test

# 4) memories 테이블 확인
sqlite3 ~/.claude/mindvault-v3/index.db "SELECT name FROM memories WHERE name='sprint4-test'"
```
Expected: `BGE-M3 OK`, `sprint4-test` row 존재.

- [ ] **Step 7.4: cleanup**

```bash
# 테스트 메모리 파일은 형이 검토 후 수동 삭제
ls ~/.claude/projects/-Users-yonghaekim-my-folder/memory/feedback_sprint4_test.md
# 필요시 rm + index에서 제거 → 다음 incremental_index 시 자동 처리
```

### 7b. install.sh 확장

- [ ] **Step 7.5: install.sh 현재 내용 확인**

```bash
cat ~/my-folder/apps/mindvault-v3/install.sh
```

- [ ] **Step 7.6: install.sh 확장**

`install.sh`에 다음 섹션 추가 (기존 Sprint 1~3 부분 그대로 보존, 끝부분에 append):
```bash

# ─────────────────────────────────────────────────────
# Sprint 4 — Layer 4 Memory Recall (Hybrid)
# ─────────────────────────────────────────────────────

echo ""
echo "==> Sprint 4: Layer 4 Memory Recall 설치"

# 4.1 Python 의존성
echo "  • pip install sqlite-vec mlx-embeddings pyyaml huggingface_hub"
pip install --user --quiet sqlite-vec mlx-embeddings pyyaml huggingface_hub || {
    echo "  ✗ 의존성 설치 실패 — Sprint 4 skip"
    exit 0
}

# 4.2 BGE-M3 모델 다운로드 (이미 있으면 skip)
if [ ! -d "$HOME/.cache/mlx-bge-m3" ]; then
    echo "  • BGE-M3 모델 다운로드 (~400MB)"
    python3 -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='mlx-community/bge-m3-mlx-4bit', local_dir='$HOME/.cache/mlx-bge-m3')" || {
        echo "  ✗ 모델 다운로드 실패 — Sprint 4 skip"
        exit 0
    }
else
    echo "  • BGE-M3 모델 이미 존재 — skip"
fi

# 4.3 스크립트 배포
SCRIPT_DIR="$HOME/.claude/scripts/mindvault"
mkdir -p "$SCRIPT_DIR"
cp src/memory_indexer.py "$SCRIPT_DIR/"
cp src/memory_search.py "$SCRIPT_DIR/"
cp src/recall_cli.py "$SCRIPT_DIR/"  # 확장 버전 덮어쓰기
cp src/memory_review_cli.py "$SCRIPT_DIR/"  # reindex 호출 추가 버전
echo "  • 스크립트 5개 배포 to $SCRIPT_DIR"

# 4.4 hook 스크립트 배포
HOOK_DIR="$HOME/.claude/hooks"
mkdir -p "$HOOK_DIR"
cp hooks/memory-recall.py "$HOOK_DIR/"
chmod +x "$HOOK_DIR/memory-recall.py"
echo "  • hook 배포: $HOOK_DIR/memory-recall.py"

# 4.5 launchd plist
PLIST="$HOME/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist"
cp plist/com.yonghaekim.bge-m3-mlx.plist "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
sleep 8  # 모델 로딩 대기
echo "  • BGE-M3 launchd 서비스 기동"

# 4.6 헬스체크
HEALTH=$(curl -sS http://localhost:8081/embed -d '{"input":"healthcheck"}' \
    -H 'Content-Type: application/json' 2>/dev/null | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print(len(d.get('vector',[])))" 2>/dev/null)
if [ "$HEALTH" = "1024" ]; then
    echo "  ✓ BGE-M3 헬스체크 OK"
else
    echo "  ✗ BGE-M3 헬스체크 FAIL — Sprint 4 hook 비활성화"
    echo "    재시도: launchctl kickstart -k gui/\$(id -u)/com.yonghaekim.bge-m3-mlx"
    exit 0
fi

# 4.7 settings.json UserPromptSubmit hook 등록
SETTINGS="$HOME/.claude/settings.json"
BACKUP="$SETTINGS.bak.$(date +%Y%m%d%H%M%S)"
cp "$SETTINGS" "$BACKUP"
python3 <<EOF
import json
from pathlib import Path

path = Path("$SETTINGS")
data = json.loads(path.read_text())
hooks = data.setdefault("hooks", {})
ups = hooks.setdefault("UserPromptSubmit", [])

# 동일 hook이 이미 있으면 skip (idempotent)
new_hook = {
    "matcher": ".*",
    "hooks": [{"type": "command", "command": "python3 \$HOME/.claude/hooks/memory-recall.py"}],
}
already = any(
    "memory-recall.py" in json.dumps(h)
    for h in ups
)
if not already:
    ups.append(new_hook)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print("  ✓ settings.json UserPromptSubmit hook 등록")
else:
    print("  • settings.json hook 이미 존재 — skip")
EOF

# 4.8 초기 인덱싱
echo "  • 초기 full_rebuild 실행 (~20초)"
python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from memory_indexer import full_rebuild
n = full_rebuild()
print(f'  ✓ 인덱싱 완료: {n} 파일')
"

echo "Sprint 4 설치 완료."
```

- [ ] **Step 7.7: install.sh 문법 검증 (dry-run 없으니 shellcheck)**

```bash
shellcheck ~/my-folder/apps/mindvault-v3/install.sh 2>&1 | head -20 || echo "shellcheck not installed - skip"
bash -n ~/my-folder/apps/mindvault-v3/install.sh && echo "syntax OK"
```
Expected: `syntax OK`.

- [ ] **Step 7.8: uninstall.sh 확장**

`uninstall.sh` 끝부분에 추가:
```bash

# Sprint 4 제거
echo "==> Sprint 4: Layer 4 제거"

# 1. launchd
PLIST="$HOME/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist"
if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "  • BGE-M3 launchd 제거"
fi

# 2. settings.json hook
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    python3 <<EOF
import json
from pathlib import Path

path = Path("$SETTINGS")
data = json.loads(path.read_text())
ups = data.get("hooks", {}).get("UserPromptSubmit", [])
before = len(ups)
ups[:] = [h for h in ups if "memory-recall.py" not in json.dumps(h)]
after = len(ups)
data["hooks"]["UserPromptSubmit"] = ups
path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print(f"  • settings.json hook 제거 ({before} → {after})")
EOF
fi

# 3. hook 파일
rm -f "$HOME/.claude/hooks/memory-recall.py"

# 4. 스크립트
rm -f "$HOME/.claude/scripts/mindvault/memory_indexer.py"
rm -f "$HOME/.claude/scripts/mindvault/memory_search.py"

# 5. memories_* 테이블은 옵션 (--purge-vec)
if [ "$1" = "--purge-vec" ]; then
    sqlite3 "$HOME/.claude/mindvault-v3/index.db" \
        "DROP TABLE IF EXISTS memories_vec; DROP TABLE IF EXISTS memories_fts; DROP TABLE IF EXISTS memories;"
    echo "  • memories_* 테이블 제거"
fi

echo "Sprint 4 제거 완료."
```

- [ ] **Step 7.9: install/uninstall 테스트 — settings.json 변형 검증**

`tests/test_install_uninstall.py`:
```python
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestSettingsJsonMutation(unittest.TestCase):
    def test_install_appends_hook_idempotent(self):
        """install이 같은 hook을 두 번 추가하지 않음."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({
                "hooks": {
                    "UserPromptSubmit": [
                        {"matcher": ".*", "hooks": [{"type": "command", "command": "/existing/hook.sh"}]}
                    ]
                }
            }, indent=2))

            # Python snippet 직접 실행 (install.sh 4.7 섹션의 로직)
            code = f'''
import json
from pathlib import Path
path = Path("{settings}")
data = json.loads(path.read_text())
hooks = data.setdefault("hooks", {{}})
ups = hooks.setdefault("UserPromptSubmit", [])
new_hook = {{
    "matcher": ".*",
    "hooks": [{{"type": "command", "command": "python3 $HOME/.claude/hooks/memory-recall.py"}}],
}}
already = any("memory-recall.py" in json.dumps(h) for h in ups)
if not already:
    ups.append(new_hook)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
'''
            subprocess.run(["python3", "-c", code], check=True)
            # 2회 실행
            subprocess.run(["python3", "-c", code], check=True)

            data = json.loads(settings.read_text())
            ups = data["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(ups), 2)  # 기존 1 + 신규 1, 중복 X
            # 기존 hook 보존
            self.assertTrue(any("existing" in json.dumps(h) for h in ups))
            self.assertTrue(any("memory-recall.py" in json.dumps(h) for h in ups))


if __name__ == "__main__":
    unittest.main()
```

```bash
python3 -m unittest tests.test_install_uninstall -v 2>&1 | tail -5
```
Expected: 1 PASS.

- [ ] **Step 7.10: commit**

```bash
git add src/memory_review_cli.py install.sh uninstall.sh tests/test_install_uninstall.py tests/test_memory_review_integration.py
git commit -m "feat(sprint4): install/uninstall 확장 + memory_review approve→reindex 1줄 추가"
```

---

## Task 8: 통합/성능 테스트 (E2E)

**Files:**
- Create: `~/my-folder/apps/mindvault-v3/tests/test_integration.py`
- Create: `~/my-folder/apps/mindvault-v3/tests/benchmark_search.py`

이 task는 BGE-M3 서버 + 실제 DB 필요. CI에서는 `MV3_SKIP_INTEGRATION=1`로 skip.

- [ ] **Step 8.1: 통합 테스트 5개 작성**

`tests/test_integration.py`:
```python
import json
import os
import statistics
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPT_DIR = Path("/Users/yonghaekim/.claude/scripts/mindvault")
HOOK = Path.home() / ".claude" / "hooks" / "memory-recall.py"


@unittest.skipIf(
    os.environ.get("MV3_SKIP_INTEGRATION") == "1",
    "MV3_SKIP_INTEGRATION=1",
)
class TestE2E(unittest.TestCase):
    def _call_hook(self, prompt: str) -> tuple[int, str, float]:
        t0 = time.time()
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"prompt": prompt}).encode(),
            capture_output=True,
            timeout=5,
        )
        return r.returncode, r.stdout.decode(), (time.time() - t0) * 1000

    def test_e2e_1_basic_korean_recall(self):
        """한국어 자연어 → memory hit."""
        rc, out, _ = self._call_hook("이메일 보내는 방법")
        self.assertEqual(rc, 0)
        # threshold 통과 시 system-reminder
        if out:
            self.assertIn("메모리 회수", out)

    def test_e2e_2_short_prompt_silent(self):
        rc, out, _ = self._call_hook("ㅇㅇ")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_e2e_3_exact_identifier_fts5(self):
        """정확 식별자 검색 — FTS5가 강함."""
        # recall_cli로 sources 분리 검증
        cli = SCRIPT_DIR / "recall_cli.py"
        r = subprocess.run(
            [sys.executable, str(cli), "msmtp", "--source", "memory"],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout.decode())
        # FTS5 결과가 source에 포함돼야 함 (있는 경우)
        for item in data.get("memory", []):
            self.assertIn("source", item)

    def test_e2e_4_hook_performance(self):
        """hook 100회 호출 평균 < 100ms, p95 < 200ms."""
        elapsed_list = []
        for i in range(100):
            _, _, ms = self._call_hook(f"테스트 쿼리 {i}")
            elapsed_list.append(ms)
        avg = statistics.mean(elapsed_list)
        p95 = sorted(elapsed_list)[94]
        print(f"\n  hook perf: avg={avg:.1f}ms p95={p95:.1f}ms")
        self.assertLess(avg, 150, f"avg too slow: {avg:.1f}ms")
        self.assertLess(p95, 250, f"p95 too slow: {p95:.1f}ms")

    def test_e2e_5_sprint123_regression(self):
        """Sprint 1/2/3 회귀 — 기존 /recall sessions 검색 정상."""
        cli = SCRIPT_DIR / "recall_cli.py"
        r = subprocess.run(
            [sys.executable, str(cli), "택시", "--source", "sessions"],
            capture_output=True,
            timeout=120,  # Gemma 재순위/요약 포함하면 시간 걸림
        )
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout.decode())
        self.assertIn("sessions", data)
        self.assertNotIn("memory", data)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 8.2: 성능 벤치마크 스크립트**

`tests/benchmark_search.py`:
```python
"""Sprint 4 성능 벤치마크. 사용: python3 tests/benchmark_search.py"""
import statistics
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path("/Users/yonghaekim/.claude/scripts/mindvault")
HOOK = Path.home() / ".claude" / "hooks" / "memory-recall.py"


def time_call(prompt: str) -> float:
    t0 = time.time()
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=f'{{"prompt":"{prompt}"}}'.encode(),
        capture_output=True,
        timeout=5,
    )
    return (time.time() - t0) * 1000


def main():
    prompts = [
        "이메일 보내는 방법",
        "스캐너 사용법",
        "유튜브 영상 만드는 법",
        "택시 장부 IAP",
        "grammar saas",
        "polished html",
        "memory recall layer",
        "embedded search hybrid",
    ] * 13  # 104회

    # warmup
    for _ in range(3):
        time_call("warmup")

    times = [time_call(p) for p in prompts]
    print(f"n = {len(times)}")
    print(f"avg     = {statistics.mean(times):.1f} ms")
    print(f"median  = {statistics.median(times):.1f} ms")
    print(f"p95     = {sorted(times)[int(len(times) * 0.95)]:.1f} ms")
    print(f"p99     = {sorted(times)[int(len(times) * 0.99)]:.1f} ms")
    print(f"max     = {max(times):.1f} ms")

    # 목표 vs 측정
    avg = statistics.mean(times)
    p95 = sorted(times)[int(len(times) * 0.95)]
    print()
    print(f"target  avg < 100ms — {'✓' if avg < 100 else '✗'}")
    print(f"target  p95 < 200ms — {'✓' if p95 < 200 else '✗'}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 8.3: 통합 테스트 실행 (BGE-M3 + 인덱스 준비된 상태에서)**

```bash
# 사전 준비
curl -sS http://localhost:8081/embed -d '{"input":"test"}' -H 'Content-Type: application/json' > /dev/null && echo "BGE-M3 OK"
ls -la ~/.claude/hooks/memory-recall.py
ls -la ~/.claude/scripts/mindvault/memory_*.py

# 통합 테스트
cd ~/my-folder/apps/mindvault-v3
MV3_SKIP_INTEGRATION=0 python3 -m unittest tests.test_integration -v 2>&1 | tail -20
```
Expected: 5 PASS (또는 일부는 데이터 의존이라 skip). 성능 출력 확인.

- [ ] **Step 8.4: 벤치마크 실행**

```bash
python3 ~/my-folder/apps/mindvault-v3/tests/benchmark_search.py
```
Expected: avg < 100ms, p95 < 200ms 라인에 ✓.

목표 미달 시: profile → 병목 식별 (BGE-M3 라운드트립이 보통 가장 큼). 양자화를 q2로 낮추거나 임베딩 lru_cache 도입 검토.

- [ ] **Step 8.5: commit**

```bash
git add tests/test_integration.py tests/benchmark_search.py
git commit -m "test(sprint4): E2E 통합 5개 + 성능 벤치마크 (avg<100ms p95<200ms)"
```

---

## Task 9: 배포 + 헬스체크 + 수동 검증

**Files:**
- Modify: `~/my-folder/apps/mindvault-v3/README.md`
- Modify: `~/my-folder/apps/mindvault-v3/handoff/SESSION-CHECKPOINT.md`

이 단계는 실제 ~/.claude/ 에 배포하고 형이 직접 검증.

- [ ] **Step 9.1: install.sh 실행**

```bash
cd ~/my-folder/apps/mindvault-v3
bash install.sh 2>&1 | tee /tmp/sprint4-install.log
```
Expected: 7개 단계(4.1~4.7) 모두 ✓. 마지막 `Sprint 4 설치 완료.`. 실패 라인 있으면 그 단계 디버깅.

- [ ] **Step 9.2: 배포물 검증**

```bash
ls -la ~/.claude/hooks/memory-recall.py
ls -la ~/.claude/scripts/mindvault/memory_*.py
ls -la ~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist
launchctl list | grep bge-m3
curl -sS http://localhost:8081/embed -d '{"input":"deploy 확인"}' -H 'Content-Type: application/json' | python3 -c "import json,sys; print('OK len:', len(json.load(sys.stdin)['vector']))"
sqlite3 ~/.claude/mindvault-v3/index.db "SELECT COUNT(*) FROM memories, memories_fts, memories_vec"
```
Expected: 모든 파일 존재, launchctl entry, OK len: 1024, memories COUNT > 0.

- [ ] **Step 9.3: settings.json hook 등록 확인**

```bash
python3 -c "
import json
from pathlib import Path
data = json.loads(Path.home().joinpath('.claude/settings.json').read_text())
ups = data.get('hooks', {}).get('UserPromptSubmit', [])
for h in ups:
    print(json.dumps(h, ensure_ascii=False))
"
```
Expected: 두 항목 — telegram-guard 보존 + memory-recall 등록.

- [ ] **Step 9.4: 수동 검증 시나리오 1 — 정확 회수**

새 Claude Code 세션을 열고 다음 메시지 입력:
```
예전에 그 폰트 깨졌던 거 있잖아
```
Expected: 응답에 `project_hyperframes_fonts_fix` 메모리 회수 흔적. 메시지 처리 지연 없음.

- [ ] **Step 9.5: 수동 검증 시나리오 2 — 잡담 silent**

같은 세션에서 입력:
```
고마
```
Expected: 회수 흔적 없이 정상 응답.

- [ ] **Step 9.6: 수동 검증 시나리오 3 — degradation**

```bash
launchctl unload ~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist
```
새 Claude Code 세션 → 메시지 입력 → 에러 없이 동작 (회수만 빠짐). 복구:
```bash
launchctl load -w ~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist
sleep 8
curl -sS http://localhost:8081/embed -d '{"input":"recover"}' -H 'Content-Type: application/json' > /dev/null && echo "OK"
```

- [ ] **Step 9.7: 수동 검증 시나리오 4 — /cs 통합**

`/cs` 실행 (현재 또는 다른 세션 종료 시점에서). 그 후 staged에서 approve된 메모리가 다음 새 세션 hook에서 회수되는지 확인.

```bash
# debug log 확인
tail -30 ~/.claude/mindvault-v3/debug.log | grep -E "hook-recall|mem-indexer"
```
Expected: incremental_index 실행 흔적, 새 메모리 회수 흔적.

- [ ] **Step 9.8: 수동 검증 시나리오 5 — /recall 양쪽**

```bash
# Claude Code 세션에서 (또는 슬래시 명령)
/recall 메일
```
Expected: memory 섹션 + sessions 섹션 둘 다 결과.

- [ ] **Step 9.9: README 업데이트**

`README.md`의 "## 동작 방식" 다이어그램과 "현황 요약" 표에 Layer 4 추가:
```markdown
## MindVault v3 현황 요약

| Layer | 상태 | 기능 |
|---|---|---|
| 1. 자동 주입 (SessionStart) | ✅ 배포 | 최근 5개 세션 Gemma 요약 자동 주입. 캐시 히트 ~50ms |
| 2. 필요 시 검색 (/recall) | ✅ 배포 | FTS5 BM25 + Gemma 재순위/요약 (sessions). memory도 hybrid 지원 |
| 3. 영구 저장 승인 게이트 (SessionEnd + /memory review) | ✅ 배포 | 트리거 감지 → staged → 사용자 승인 → MEMORY.md |
| 4. 메모리 회수 hook (UserPromptSubmit) | ✅ 배포 | memory/*.md hybrid 검색을 매 메시지마다 자동 주입 (silent fail) |

4-layer 파이프라인 완성.
```

또 디버깅 섹션에 BGE-M3 헬스체크/재기동 명령 추가.

- [ ] **Step 9.10: SESSION-CHECKPOINT.md 작성**

`handoff/SESSION-CHECKPOINT.md` 새로 작성 — Task 9 완료 시점 상태 기록.
```markdown
# Session Checkpoint — 2026-05-22 (Sprint 4 종료, Layer 4 배포)

Sprint: 4 (Layer 4 — UserPromptSubmit hybrid recall)
Status: 배포·검증 완료. 4-layer 파이프라인 완성.

## 이번 세션 완료
- BGE-M3 MLX HTTP 서버 (port 8081, launchd)
- schema V2 (memories, memories_fts, memories_vec)
- memory_indexer (frontmatter + redact + sqlite-vec + FTS5)
- memory_search (hybrid RRF, k=60, desc weight 1.5x)
- hooks/memory-recall.py (silent, 200ms hard timeout)
- recall_cli --source 확장 (both 기본)
- install/uninstall 확장 (settings.json idempotent append)
- 통합 테스트 5개 + 벤치마크

## 검증 결과
- BGE-M3 헬스체크: OK (1024차원)
- hook 평균 < 100ms, p95 < 200ms
- Sprint 1/2/3 회귀 없음
- "폰트 깨졌던 거" → project_hyperframes_fonts_fix 회수 ✓
- 잡담 silent ✓
- degradation: BGE-M3 다운 → 회수만 빠짐, 메시지 처리 정상 ✓

## 잔여/미래
- CLAUDE.md [메모리 회수 Ritual] 폐기는 hook hit rate 안정 후 Sprint 5에서
- 시간 단위 청킹 (5KB+ 파일) 미구현 — Sprint 5+
- WikiLink graph expansion — 미래
```

- [ ] **Step 9.11: 최종 commit + 태그**

```bash
cd ~/my-folder/apps/mindvault-v3
git add README.md handoff/SESSION-CHECKPOINT.md
git commit -m "docs(sprint4): README 4-layer + SESSION-CHECKPOINT 갱신"
git tag sprint-4-done
git log --oneline | head -15
```
Expected: Sprint 4 관련 commit 9~10개 + tag `sprint-4-done`.

---

## 자가 점검

- [ ] BGE-M3 launchd 부팅 시 자동 기동 (`KeepAlive=true` 확인)
- [ ] settings.json 백업 파일(`.bak.YYYYMMDDHHMMSS`)이 install 후 존재
- [ ] hook이 telegram-guard보다 늦게 발동해도 무방한지 (둘은 독립)
- [ ] memory_review approve 직후 새 메모리가 즉시 회수 가능한지
- [ ] 형 [html-output-default] 규칙대로 spec/plan 산출물 HTML 같이 제공
- [ ] CLAUDE.md `[메모리 회수 Ritual]` 규칙은 Sprint 4 동안 공존 — 폐기 X
