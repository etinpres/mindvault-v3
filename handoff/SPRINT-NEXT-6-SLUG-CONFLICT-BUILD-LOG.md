---
name: handoff-sprint-next-6-slug-conflict
description: V3-NEXT-IMPROVEMENTS #6 — session 안 동일 slug 다중 candidate 정보 손실 해결. body 동일 → dedup, body 다름 → _2/_3 suffix. write_staged 에 slug_override 인자 추가, 충돌 처리는 _stage_with_conflict_resolution 헬퍼로 분리.
---

MindVault v3 → 차기 보강 #6 — slug conflict 해결 빌드 로그

## 요약

V3-NEXT-IMPROVEMENTS #6 해결. SPRINT-14-BUILD-LOG 미해결 4번. SessionEnd 안에서 같은 slug candidate 가 둘 이상 추출되면 기존 dedup 로직이 정보 손실로 처리됐다 (slugs.add 후 다음 같은 slug 는 무조건 skip). 형이 한 세션에서 같은 주제를 다른 측면으로 두 번 언급한 경우 두 번째가 사라짐.

master HEAD `7e39f6b` (NEXT-5 diff color) 기준 worktree `worktree-next-6-slug-conflict`.

## 자율 결정 사유

- **body 동일 → dedup, body 다름 → suffix** — 가장 단순한 정보 보존 룰. body 같으면 stage 두 번 할 의미 없음 (review 시 같은 내용 두 번). body 다르면 형 검토 단계에서 어느 쪽 더 정확한지 판단 가능 → suffix 로 모두 살림.
- **suffix 패턴 `_2`, `_3`** — timestamp 외 인덱스. `slugify` 결과의 base 는 30자 cap 안이라 suffix 추가 후에도 OS path 길이 문제 없음.
- **file system 기존 slug 충돌 + update_of 없음 → skip 보존** — 기존 동작. `_2` suffix 로 양산하면 file overwrite 는 안 되지만, "예전에 정해놓은 메모리와 사실상 같은 주제인데 staged 가 살아남" 케이스를 막기 위해 skip 유지. 형이 직접 신규 stage 시 명시.
- **기존 slug 충돌 + update_of 있음 → 통과** — Sprint 14 compile flow 가 책임. compiler 가 같은 path 와 매칭한 경우 `update_of` 메타 부착 → diff/approve 거쳐 정상 overwrite.
- **충돌 로직 별도 함수 추출** — `_stage_with_conflict_resolution(candidates, existing_slugs_set, sid, writer)` 분리. main() 의 jsonl/Gemma 통합 흐름과 무관하게 단위 테스트 가능. writer 콜백 패턴 (writer(item, sid, slug_override=...)) 으로 file I/O 의존 0.
- **write_staged 에 `slug_override` 인자 추가** — slugify 가 함수 내부 호출이라 외부에서 final slug 통제 못 했음. 옵션 인자로 추가, default 는 None (기존 호출 영향 0).

## 변경 상세

### A. `src/session_memory_end.py`

- `write_staged(item, session_id, slug_override=None)` 시그니처 변경. None 이면 `slugify(item["title"])` (기존 동작).
- `_stage_with_conflict_resolution(candidates, existing_slugs_set, session_id, writer) -> int` 신규. main() 의 충돌 처리 블록을 추출. session_slug_bodies dict 로 본문별 추적, suffix 부여.
- main() 의 staging loop 를 한 줄 호출로 단순화:
  ```python
  written = _stage_with_conflict_resolution(candidates, slugs, sid, write_staged)
  ```

핵심 룰 (요약):
1. `s_base in existing_slugs_set and not item.update_of` → skip
2. body 가 session_slug_bodies 의 같은 base 에 이미 있음 → skip (dedup)
3. session_slug_bodies 에 base 가 처음 → `s_final = s_base`
4. session_slug_bodies 에 base 가 있음 → `s_final = f"{s_base}_{count+1}"`

### B. 테스트 (`tests/test_procedural_slot.py` 의 새 `TestSlugConflictResolution`)

| 테스트 | 검증 |
|---|---|
| test_distinct_slugs_all_stage | 다른 slug 2건 → 모두 stage |
| test_same_slug_different_body_suffix | 같은 slug + 다른 body 3건 → `same`, `same_2`, `same_3` |
| test_same_slug_same_body_deduped | 같은 slug + 같은 body 두 번 → 한 개만 |
| test_existing_memory_collision_no_update_of_skip | 기존 slug + update_of 없음 → skip |
| test_existing_memory_collision_with_update_of_allows | 기존 slug + update_of 있음 → stage |
| test_writer_failure_does_not_block_progression | writer False 반환해도 다음 candidate 처리 |

## 측정 데이터

### procedural_slot 단독

```
30/30 PASS (0.03s)
신규 6건: TestSlugConflictResolution.*
기존 24건 보존
```

### 전체 회귀

```
225/227 PASS (test_install_uninstall 제외, 103s)
2 fail = test_schema_v2.* — master HEAD `7e39f6b` 동일 pre-existing.
```

신규 회귀 0건.

### 운영 효과 예상

- Sprint 14 BUILD-LOG 미해결 4번의 "두 번째 staged 가 사라짐" 시나리오 → 자동 suffix 양산. 형의 review 부담은 +α (같은 slug 2~3개 보고 결정해야 함) 이지만 정보 손실은 0.
- body 동일 dup 도 자연스러운 dedup — extractor 가 같은 turn 을 두 번 추출하는 우연 케이스 자동 제거.

## 안전 정책 준수

- `indexer.full_rebuild()` 호출 없음.
- Sprint 10 트랜잭션 패턴 무변경.
- BGE plist / `bge_m3_server.py` 무변경.
- launchctl 서비스 무관.
- write_staged 의 기존 caller (없음, 본 모듈 내부만) 영향 0 — slug_override default None.
- worktree 격리.

## 미해결 / 다음 #7

- **suffix 카운터 _1 시작 vs _2 시작** — 본 구현은 base + _2/_3/_4. base 가 _1 역할. 운영 누적 후 형이 "_1, _2, _3" 표기 선호하면 단순 변경.
- **30자 + suffix 길이 합산** — slugify 가 30자 cap 인데 suffix 추가하면 31~33자. 파일명 길이 문제 없음. NTFS/HFS+ 모두 255 byte 안 = OK.
- **#7 self_eval scan latency** — 다음 sprint.

## 변경 파일

```
src/session_memory_end.py                                | +35 -10
tests/test_procedural_slot.py                            | +95
handoff/SPRINT-NEXT-6-SLUG-CONFLICT-BUILD-LOG.md         | 신규
```
