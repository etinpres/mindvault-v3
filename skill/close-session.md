---
description: "[mv3-skill] 현재 세션의 새 사실·결정·노하우를 MindVault v3 메모리에 명시 반영 (자동 hook 보완)"
argument-hint: [--dry-run]
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

사용자가 `/close-session` (또는 alias `/cs`) 를 호출했다. SessionEnd hook의 자동 Memory Compiler (Gemma 기반) 가 procedural 사실은 잘 추출하지만, **narrative project memory** (왜 그렇게 결정했는지, 여러 단계의 맥락 종합) 는 약하다. 이 skill 은 컨텍스트 살아있는 메인 Claude 가 직접 회고해 그 갭을 메운다.

## 운영 철학

- **자동 hook 으로 만들지 않는다.** Karpathy LLM Wiki 첫 실무 적용 사례(dev.to)에서 자동 session-end hook 이 약 50% silent failure 기록. 사용자가 명시 호출하는 단일 명령으로만 동작한다.
- **현재 컨텍스트 살아있을 때 직접 요약.** transcript JSON 재파싱이 아니라 메인 Claude 가 이번 세션의 핵심 변경을 회상해 작성.
- **추출 0건이면 그냥 종료.** 억지로 만들지 말 것 — 빈 append 가 인덱스 노이즈가 된다.

## 실행 순서

### 1. 메모리 슬롯 결정

MindVault v3 는 cwd 별로 별도 `~/.claude/projects/<slug>/memory/` 슬롯을 만든다. 쓰기 destination 은 다음 우선순위로 결정:

```bash
PROJECTS_ROOT="$HOME/.claude/projects"

# 모든 결정된 MEM_DIR 은 PROJECTS_ROOT 하위인지 realpath 로 검증 (boundary 강제)
# audit-2026-05-25 round-3 C: $root 자체는 명시 reject — `case .. */*` 가 empty 매칭
# 가능성 차단. 또 끝의 trailing slash 도 안전하게 처리.
_validate_mem_dir() {
  local d="$1"
  [ -d "$d" ] || return 1
  local abs root
  abs=$(cd "$d" 2>/dev/null && pwd -P) || return 1
  root=$(cd "$PROJECTS_ROOT" 2>/dev/null && pwd -P) || return 1
  # $root 자체는 메모리 슬롯 아님 (그 안의 <slug>/memory/ 가 슬롯)
  [ "$abs" = "$root" ] && return 1
  case "$abs/" in
    "$root"/) return 1 ;;       # 안전망: 위 검사가 빠뜨려도 root 거부
    "$root"/*) return 0 ;;      # PROJECTS_ROOT/<무엇이든> 통과
    *) return 1 ;;
  esac
}

# 우선순위 1: env override (단 PROJECTS_ROOT 하위 강제)
MEM_DIR=""
if [ -n "${MV3_MEMORY_DIR:-}" ]; then
  if _validate_mem_dir "$MV3_MEMORY_DIR"; then
    MEM_DIR="$MV3_MEMORY_DIR"
  else
    echo "⚠️  MV3_MEMORY_DIR='$MV3_MEMORY_DIR' 가 $PROJECTS_ROOT 밖이거나 없음 — 무시" >&2
  fi
fi

# 우선순위 2: 현재 cwd 슬러그 (Claude Code 가 실제 jsonl 을 쌓는 곳)
if [ -z "$MEM_DIR" ]; then
  CWD_SLUG=$(pwd | sed 's|/|-|g')
  CWD_MEM="$PROJECTS_ROOT/${CWD_SLUG}/memory"
  [ -d "$CWD_MEM" ] && MEM_DIR="$CWD_MEM"
fi

# 우선순위 3: 가장 최근 갱신된 MEMORY.md (NUL-separated, 공백·`:` path 안전)
if [ -z "$MEM_DIR" ]; then
  newest=$(find "$PROJECTS_ROOT" -maxdepth 3 -name 'MEMORY.md' -type f -print0 2>/dev/null \
    | xargs -0 stat -f '%m %N' 2>/dev/null \
    | sort -rn | head -1 | sed 's/^[0-9]* //')
  if [ -n "$newest" ]; then
    candidate=$(dirname "$newest")
    _validate_mem_dir "$candidate" && MEM_DIR="$candidate"
  fi
fi

# 우선순위 4: 첫 호출이면 cwd 슬롯 자동 생성
if [ -z "$MEM_DIR" ]; then
  CWD_SLUG=$(pwd | sed 's|/|-|g')
  MEM_DIR="$PROJECTS_ROOT/${CWD_SLUG}/memory"
  mkdir -p "$MEM_DIR"
  [ ! -f "$MEM_DIR/MEMORY.md" ] && printf '# Memory Index\n\n' > "$MEM_DIR/MEMORY.md"
fi

# 최종 가드: 결정된 MEM_DIR 이 PROJECTS_ROOT 하위인지 한 번 더
_validate_mem_dir "$MEM_DIR" || {
  echo "FATAL: MEM_DIR='$MEM_DIR' 이 $PROJECTS_ROOT 밖. 중단." >&2
  exit 1
}

TODAY=$(date +%Y-%m-%d)
```

결정된 `$MEM_DIR` 을 사용자에게 한 줄 보고. 예상과 다르면 사용자가 `MV3_MEMORY_DIR=...` 로 override 가능함도 안내.

### 2. 인자 파싱

- `--dry-run` 이 인자에 있으면 `DRY_RUN=1`. 파일 수정 없이 작성 내용만 출력.
- 그 외 인자는 무시 (`/close-session 정리해줘` 같은 자연어 인자 허용, 단 처리는 동일).

### 3. 이번 세션 회고 (메인 Claude 직접 수행)

이번 세션에서 발생한 다음 5 카테고리를 한 줄씩 추출:

1. **project** — 프로젝트 상태 변경, 빌드/배포 결과, 새 발견. 예: "tag v3.0.2 release Latest 부착"
2. **feedback** — 사용자가 명시적으로 정정·확인한 워크플로 규칙. 예: "큰 마이그레이션 후 codex 독립 검증"
3. **user** — 사용자 자신의 역할·전문성·선호 신규 정보. 예: "전직 영어 교사"
4. **reference** — 외부 시스템 단서 (URL, 계정, API 위치 등)
5. **procedural** — 명령·command·휴리스틱·재사용 가능한 노하우. 예: "PRAGMA WAL 은 DB 파일 속성이라 1회 init 으로 충분"

각 항목 형식: `[YYYY-MM-DD] <한 줄 요약>` (bold 표기 없음).

**추출 0건이면 사용자에게 "이번 세션에 저장할 새 항목 없음. 종료." 출력 후 exit.**

### 4. 토픽 파일 매칭

각 항목별로:

```bash
# 5 type 모두 검색 (procedural 포함)
matches=$(grep -l "$KEYWORD" "$MEM_DIR"/{project,feedback,user,reference,procedural}_*.md 2>/dev/null)
```

- 매칭 1건 → 해당 파일 끝에 `[YYYY-MM-DD] <요약>` append (bold 없음)
- 매칭 0건 → 신규 토픽 파일 생성. 네이밍: `<type>_<slug>.md`
  - **slug sanitize 의무**: 정규식 `^[a-z][a-z0-9_-]{0,31}$` 통과만 허용. 경로 구분자(`/`, `\`), 상대경로(`..`), shell metachar 모두 차단. 사용자가 한국어/특수문자 슬러그 입력 시 메인 Claude 가 영문 kebab 으로 변환 후 검증.
  - type 도 화이트리스트 5개(`user|feedback|project|reference|procedural`) 외 거부.
  - 최종 경로는 `realpath` 로 `$MEM_DIR` 하위인지 확인 후 쓰기 (traversal 방어 in-depth).
- 매칭 2건 이상 → 사용자에게 한 줄로 어느 파일에 넣을지 선택 요청. 인터랙티브 불가 환경에서는 가장 최근 mtime 파일에 default append + 사용자에게 알림

### 5. 신규 파일 프론트매터

YAML 본문은 **double-quoted scalar** 로 작성해 metachar(`:`, `#`, `&`, `*`, `[`, `]`, newline) injection 방어. 메인 Claude 는 description 본문에서 `"` 와 `\` 를 각각 `\"`, `\\` 로 escape.

```markdown
---
name: <short-kebab-slug>
description: "<한 줄 요약 — 미래 회수 매칭 키, 200자 이내, double-quoted>"
metadata:
  type: <user | feedback | project | reference | procedural>
---

[YYYY-MM-DD] <본문>

Why: <왜 이 결정·사실이 중요한가>
How to apply: <앞으로 어떤 상황에서 활용할지>
```

`name` 은 §4 의 sanitize 통과한 slug 그대로 (이미 safe charset). `description` 은 위 escape rule 적용.

`description` 길이 200자 초과 시 잘라낸 뒤 사용자에게 알림. 너무 길면 회수 시 noise.

기존 파일 append 시엔 프론트매터 건드리지 말고 본문 끝에 새 라인 추가:
```markdown

[YYYY-MM-DD] <업데이트 내용>
```

### 6. 민감정보 leak 가드

신규 파일 작성 또는 기존 파일 append 직전, 다음 정규식을 본문 + frontmatter description 에 매칭:

```
(sk-[A-Za-z0-9]{20,}|api[_-]?key[\s:=]+\S+|password[\s:=]+\S+|token[\s:=]+\S+|Bearer\s+[A-Za-z0-9\-_]+|ghp_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})
```

**매칭 시 작성 중단.** 값 자체 저장은 절대 금지.

위치만 기록도 신중해라 — 정확한 파일경로 + 형식까지 묻혀서 후속 leak 트리거가 될 수 있다. 일반화 표현만 허용:
- ❌ `Anthropic API key 는 ~/.config/anthropic/credentials.json line 3`
- ✅ `<provider> credentials 는 사용자 환경설정에 위치`

운영 비밀이 메모리에 들어가야 하는 경우 → 메모리 시스템 밖 (예: macOS Keychain, 1Password) 으로 유도.

### 7. 동시성 가드 (file lock)

`/close-session` 실행 중 SessionEnd hook 의 자동 Memory Compiler 가 같은 `$MEM_DIR` 에 동시 쓰기 할 수 있다 (race → interleave 또는 lost update). 모든 write 전 lock 획득:

```bash
LOCK_FILE="$MEM_DIR/.close-session.lock"
exec 9>"$LOCK_FILE"
flock -x -w 30 9 || { echo "lock acquire failed (30s timeout) — 다른 writer 활동 중"; exit 1; }
# … 이 안에서 신규 파일 작성, MEMORY.md append 등 모든 write 수행 …
# fd 9 가 닫히면 lock 자동 해제 (process 종료 또는 명시 exec 9>&-)
# write 완료 후 lock 파일 자체 정리 (stale 누적 방지)
trap 'exec 9>&-; rm -f "$LOCK_FILE"' EXIT
```

신규 토픽 파일 생성도 **atomic rename + race detect** 패턴:

```bash
target="$MEM_DIR/${type}_${slug}.md"
tmp=$(mktemp "$MEM_DIR/.tmp.XXXXXX")
printf "%s" "$content" > "$tmp"
if [ -e "$target" ]; then
  # race: 다른 writer 가 lock 획득 전에 같은 슬러그 생성. 우리 본문 살리고 사용자 알림.
  rescue="$MEM_DIR/${type}_${slug}.conflict-$(date +%s).md"
  mv "$tmp" "$rescue"
  echo "⚠️  slug 충돌 — 내용을 $rescue 에 보관. 사용자가 수동 머지 필요." >&2
else
  mv "$tmp" "$target"  # `mv -n` 대신 명시 if-test 로 silent loss 방지
fi
```

### 8. MEMORY.md 인덱스 갱신

§7 의 lock 안에서 진행. 신규 토픽 파일을 만들었다면 `$MEM_DIR/MEMORY.md` 끝에 1줄 추가 (flat list 패턴):

```markdown
- [<제목>](<파일명>.md) — <한 줄 hook>
```

콜론(`:`) 아니라 **dash(`—`) 사용**. 기존 파일 append 한 경우 인덱스 갱신 불필요.

`MEMORY.md` 가 200줄 넘으면 경고 출력. 인덱스 truncation 위험 (메모리 회수 hook 이 200줄 초과분 무시할 수 있음).

### 9. 요약 보고

```
✅ /close-session 완료 [YYYY-MM-DD]
- slot: $MEM_DIR
- project: N건 (파일 X, Y)
- feedback: N건 (파일 Z)
- procedural: N건
- user / reference: N건
MEMORY.md: M줄 (한계 200)
```

`DRY_RUN=1` 인 경우 파일 수정 없이 "[DRY-RUN] 위 내용이 작성될 예정" 한 줄 추가.

## 안전 규칙

- **`Edit replace_all` 금지** — 기존 항목 덮어쓰기 위험. 항상 append.
- **삭제·수정 금지** — 옛 사실이 stale 해 보여도 supersession 표시만. 예: `[2026-05-13] 위 정책은 deprecated. 새 정책 → [[link]]`
- **자동 hook 절대 만들지 말 것** — `ScheduleWakeup`/`cron` 가능하지만 silent failure 위험으로 비추.
- **drift 점검** — 신규 파일 작성 후 반드시 `ls` 로 파일 존재 확인.
- **민감 정보 차단** — API 키·비밀번호·토큰은 메모리에 절대 쓰지 말 것. 위치만 기록 (§6 가드 참조).

## 실패 모드

- 메모리 디렉토리 missing → §1 우선순위 4 가 자동 생성 후 진행
- 토픽 매칭 2건 이상이고 인터랙티브 불가 → 가장 최근 mtime 파일 default + 사용자에게 알림
- 추출 0건 → 그냥 종료 (빈 append 절대 금지)
- MEMORY.md 200줄 초과 → 경고만 출력, 자동 슬림화 X (사용자 결정 영역)
- `$MEM_DIR` 가 여러 슬롯에 존재 (multi-slot drift) → 결정한 단일 슬롯만 갱신하고 다른 슬롯 존재 사실을 보고 (사용자가 통합 결정)

## 짝 스킬

- `/recall <검색어>` — 과거 세션·메모리 풀텍스트 검색 (Layer 2+4 hybrid, 실측 200ms)
- `/memory_review` — Memory Compiler 가 자동 추출한 `_procedural/_staged/` 후보를 사용자가 검토·승인
- 메모리 회수: UserPromptSubmit hook 이 자동 처리 (`~/.claude/CLAUDE.md "메모리 회수 (자동화됨)"` 참조). `/close-session` 은 그 반대 방향 — 세션 **종료** 시 명시 반영.
