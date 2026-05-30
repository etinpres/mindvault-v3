# agentmemory 경쟁 분석 → MindVault 차용 후보

- **작성일**: 2026-05-30
- **상태**: Draft (차용 후보 목록 — 채택은 각 Phase 구현 계획에서 게이트)
- **현재 기준 버전**: v3.4.1 (`eda596e`)
- **상위 문서**: [세컨드 브레인 로드맵](./2026-05-30-second-brain-roadmap-design.md), [Phase 1 provenance](../plans/2026-05-30-phase1-provenance.md)
- **분석 출처 영상**: `https://youtu.be/hqcZZuvBUSY` (Context Engineering 도구 5선)
- **대상 repo**: https://github.com/rohitg00/agentmemory (19,679 stars, 생성 2026-02-25, v0.9.24)

---

## 0. 한 줄 요약

agentmemory는 사실상 **"공개 제품화된 MindVault"**다 (우리 메모리 [[llm-wiki-pattern]]이 이미 "v2 확장"으로 북마크). 대부분 축에서 더 넓고(자동수집·생태계·견인), 우리 해자는 좁지만 진짜다(한국어 Arctic-ko · Gemma 의미 모순감지 · 자기완결 markdown+git · README 정직성). 본 문서는 **자기만족 고정관념을 배제하고**, agentmemory에서 *우리 제약을 지키며 차용 가능한 부분만* 추린다.

**불변 제약 (차용 필터)** — [[feedback-notion-not-memory-backend]] + 로드맵 §6.1:
- Claude Code 내부에서만 작동 (별도 서버/에이전트 금지 → **agentmemory의 독점 `iii-sdk` 런타임은 차용 불가**)
- 운영비 0 (로컬 MLX: Gemma :8080 + Arctic-ko :8081)
- 마크다운 + git = source of truth (in-memory Map + JSON snapshot 모델 차용 불가)
- v1 토큰낭비 금지 (false negative > false positive, raw cosine 절대 게이트 유지)

---

## 1. 정직한 비교 스냅샷

| 축 | MindVault v3.4.1 | agentmemory | 판정 |
|---|---|---|---|
| 자동 수집 | SessionEnd staging + 수동 `/close-session` + SessionStart 주입 | 12+ 라이프사이클 hook 자동 (PreToolUse/PostToolUse/**PreCompact 재주입**) | **그쪽 우위** — 최대 갭 |
| 검색 | FTS5 + Arctic-ko vector RRF(2-stream) + wikilink 1hop | BM25 + vector + **graph traversal** RRF(3-stream), RRF_K=60 | 그쪽 약간 우위 |
| 한국어 | Arctic-ko v2.0 1024dim 특화 | MiniLM-L6-v2 384dim 영어중심, 일반 CJK seg | **우리 우위** |
| 모순감지 | Gemma 4-way **LLM 의미** 분류 + deprecated_by ×0.3 decay | 어휘 **Jaccard >0.9** soft-supersede | **우리 우위** |
| 망각 | deprecated_by decay | hard 180일 & importance≤2 cutoff (README "Ebbinghaus"는 **코드와 모순 = 거짓**) | 무승부, 단 그쪽 README 거짓 |
| 저장/견고성 | markdown+git+SQLite FTS5+sqlite-vec, atomic write, 570 pytest | in-memory Map + JSON snapshot(5s debounce, crash 시 유실) + **독점 iii-sdk** | **우리 우위** |
| 스케일 천장 | `_vec_top_k` 전체 numpy 로드(~수백개 전제) | brute-force 선형 스캔, ANN 없음 | **무승부 — 양쪽 미해결** |
| 생태계 | CC 내부 전용 | MCP 53툴, 8+ 에이전트, REST | 그쪽 압도 (단 우리 제약상 비범위) |
| 정직성 | no-future-predictions 규율 | README가 코드와 모순(SQLite·Ebbinghaus) | **우리 우위** |

**결론**: MindVault는 명백히 우월하지 않다. 단일 최대 갭은 **자동수집 커버리지**. 그 외엔 *패턴*만 골라 차용한다.

---

## 2. 차용 후보 (우선순위순) — 로드맵 Phase 매핑

### ★ A. PreCompact 재주입 hook — **최우선 / 즉시 검토 가치**
- **agentmemory**: 컨텍스트 compaction 직전 `PreCompact` hook에서 관련 메모리를 다시 주입해 "압축으로 인한 망각"을 막음.
- **왜 우리에게**: 영상의 "80% 손실" 문제 직격. 우리는 SessionStart 주입은 있으나 **세션 중간 compaction 시 재주입이 없다** → 긴 세션에서 초반 회수 맥락이 compaction에 휩쓸려 사라짐. 이건 순수 **Claude Code 네이티브 hook**이라 우리 "CC 내부 전용" 제약과 100% 호환, 운영비 0.
- **우리식 적용**: `PreCompact` hook 추가 → 현재 세션 active 메모리(Layer 4가 이미 주입한 항목)를 raw cosine 게이트 통과분만 재주입. v1 토큰낭비 회피 위해 TOP_K 소량 + 출처 라벨만(원문 X).
- **Phase 매핑**: Phase 0~1 보강 (토대). recall 통합률(strict cited 7.62%) 개선과 직접 연결 — 회수가 살아있어야 통합된다.
- **차용 비용**: 낮음. hook 1개. **단일 최고 ROI 후보.**

### B. Graph traversal을 RRF 1급 스트림으로 승격
- **agentmemory**: BM25 + vector + **graph(entity traversal)** 3-stream RRF. 그래프가 후처리가 아니라 독립 rank로 융합.
- **왜 우리에게**: 우리는 `[[wikilink]]`를 이미 풍부히 쓰고 회수도 `wikilink-1hop`으로 일부 한다(회수 로그 `score 0.00, wikilink-1hop` 확인). 하지만 1hop은 *후처리 확장*이지 RRF 가중 스트림이 아니다. 승격하면 [[link]] 자산을 제대로 활용.
- **우리식 적용**: wikilink 그래프를 3번째 RRF 스트림으로(가중치 보수적, FTS/vector 우선). RRF_K=60 참고. 기존 `_vec_top_k`/FTS 파이프라인에 stream 추가.
- **Phase 매핑**: **Phase 2(지식 구조)**. Phase 2가 이미 계획한 타입 엣지(supports/extends/refutes/depends_on)와 자연 결합 — 타입 엣지가 생기면 graph 스트림의 신호가 풍부해짐.
- **차용 비용**: 중간. RRF에 stream 추가 + 가중 튜닝 + 회귀.

### C. 타입 엣지 + 경량 supersession (bi-temporal의 *핵심만*)
- **agentmemory**: bi-temporal 엣지(`tcommit`/`tvalid`/`tvalidEnd`) + `supersededBy` + edge history + as-of-time 쿼리.
- **왜 우리에게**: 우리 deprecated_by는 "감쇠"만 한다. agentmemory의 `supersededBy` 포인터 + valid 윈도우는 "언제부터/까지 참이었나"를 표현해 **stale over-trust**(로드맵 §1.2)를 구조적으로 누른다.
- **우리식 적용 — 경량만 차용**:
  - Phase 1 provenance의 `source.ts`에 더해 fact에 `valid_from`/`valid_until`(optional) + `superseded_by: [name]` 포인터.
  - **풀 bi-temporal as-of 쿼리 엔진은 차용 안 함** — 수백개 규모엔 과설계. 포인터 + valid 윈도우까지만.
- **Phase 매핑**: Phase 1(provenance/stale) + Phase 2(타입 엣지).
- **차용 비용**: 낮음(frontmatter 필드) ~ 중간(stale 재검증 로직 연동).

### D. 외부 표준 벤치마크(LongMemEval-S) 도입 — Phase 1 오픈 질문 해소
- **agentmemory**: "95.2% R@5 on LongMemEval-S (500q)" 자기보고(방법론 미감사). LongMemEval은 장기 메모리 회수의 공개 표준 벤치.
- **왜 우리에게**: 로드맵 **§8 오픈 질문 "recall strict cited 목표 수치"**가 미정이다. 우리 `self_eval` strict-cited(7.62%)는 *내부* 지표라 절대 비교가 어렵다. LongMemEval-S 같은 외부 표준을 보조 지표로 깔면 목표치를 객관적으로 잡고 경쟁자와 같은 자 위에 설 수 있다.
- **우리식 적용**: 한국어 메모리 특성상 영어 벤치 그대로는 부적합 → LongMemEval *프로토콜*(질문셋 구조·R@K 측정법)만 차용해 우리 한국어 메모리로 소규모 골든셋 구성. self_eval과 병행.
- **Phase 매핑**: Phase 1 완료 게이트의 정량 목표 확정.
- **차용 비용**: 중간. 골든셋 구축 + harness.

### E. importance 스코어 + forget_after TTL (선택적 메타)
- **agentmemory**: observation에 `importance`(1–10, default 5) + 메모리에 `forgetAfter` TTL.
- **왜 우리에게**: 우리 decay는 deprecated_by에만 걸린다. 일부 메모리(예: "이번 sprint 한정" 임시 사실)는 명시적 만료가 자연스럽다.
- **우리식 적용**: staged 메모리에 optional `importance` 힌트 + `forget_after` 날짜. **agentmemory처럼 "Ebbinghaus"라 과대포장 금지** — hard cutoff면 hard cutoff라 정직히 문서화([[no-future-release-predictions]] 정신).
- **Phase 매핑**: Phase 1 신뢰성.
- **차용 비용**: 낮음. 단 over-engineering 경계(대부분 메모리는 TTL 불필요).

### F. PII 필터 on capture (경량 위생)
- **agentmemory**: `UserPromptSubmit` 캡처 시 privacy-filter.
- **왜 우리에게**: 우리도 세션 텍스트를 staging한다. 공개 가능성([[public-ship-sanitize-pattern]]) 있는 시스템이라 캡처 시점 경량 PII/호칭 필터가 사후 sanitize 부담을 줄임.
- **우리식 적용**: staging writer에 경량 redaction(이메일/전화/호칭). 기존 sanitize sweep과 중복 회피.
- **차용 비용**: 낮음.

---

## 3. 명시적 비차용 (그리고 이유)

| 항목 | 비차용 이유 |
|---|---|
| **독점 `iii-sdk` 런타임** | "CC 내부 전용" + 운영비 0 + markdown/git source-of-truth 제약 정면 위반. 단일 벤더 substrate 의존은 v1 silent-failure 위험 재현 |
| in-memory Map + JSON snapshot(5s debounce) | 우리 SQLite FTS5 + sqlite-vec + atomic write가 더 견고. crash 유실 위험 도입 안 함 |
| MiniLM 영어 임베딩 | Arctic-ko가 우리 한국어 데이터에 우월. 다운그레이드 |
| 어휘 Jaccard 모순감지 | 우리 Gemma 4-way LLM 의미 분류가 우월. 차용 이유 없음 |
| 4-tier consolidation/crystallization (Working→Episodic→Semantic→Procedural 자동 압축) | [[alex-second-brain-video]] 분석에서 "세션연속성 기여 낮음 + codex 미강조"로 이미 보류. agentmemory가 *구현*은 했으나 회수 품질 개선은 자기보고 벤치뿐(미검증). **수요는 검증, 채택은 보류 유지** |
| 풀 bi-temporal as-of 쿼리 엔진 | 수백개 규모 과설계. supersededBy 포인터 + valid 윈도우(§2-C)까지만 |
| MCP 53툴 / 멀티 에이전트 외부화 | CC 내부 전용 제약상 비범위 |

---

## 4. 권장 채택 순서 (게이트 통과 전제)

1. **A. PreCompact 재주입** — Phase 1 보강, 최고 ROI, hook 1개. *먼저.*
2. **D. LongMemEval 프로토콜** — Phase 1 목표치(오픈 질문) 해소에 필요.
3. **C. 경량 supersession 포인터 + E. forget_after** — Phase 1 provenance와 함께.
4. **B. graph RRF 스트림** — Phase 2 타입 엣지와 동시.
5. **F. PII 필터** — 여유 시 위생 개선.

각 항목은 로드맵 원칙대로 **검증 가능한 완료 게이트**로 닫고 빅뱅 금지. 채택은 본 문서가 아니라 각 Phase 구현 계획에서 확정한다.

---

## 5. 메타 교훈 (자기만족 배제 결과)

- 우리가 "v3.4 모순감지 = 해자"라 믿었으나, agentmemory가 (조잡하게나마) 어휘 Jaccard로 동일 기능을 *공개로 굴린다*. 우리 우위는 "더 정교할 뿐 유일하진 않다."
- 진짜 갭은 **자동수집 커버리지**(PreCompact 재주입)다 — 화려한 consolidation/graph가 아니라.
- agentmemory의 README가 자기 코드를 거짓 설명(SQLite·Ebbinghaus)하는 건, *우리* no-future-predictions·정직성 규율의 가치를 역으로 증명한다. 차용하되 그들의 과대포장 습관은 차용하지 않는다.


---

## 6. 구현 진행 (이 문서 이후)

### ★ A. PreCompact 재주입 — **구현 완료 (2026-05-30, master `6d86d49`)**
- **메커니즘 정정**: PreCompact hook 은 *압축 이후 컨텍스트에 살아남는* additionalContext 를 주입할 수 없다(공식: decision 필드만). 실제 경로는 **SessionStart hook + `source="compact"`** — 압축 직후 SessionStart 가 재발화하며 `hookSpecificOutput.additionalContext` 가 fresh 컨텍스트에 남는다. 우리 SessionStart 는 이미 `matcher="*"` 라 compact source 도 들어와 **등록 변경 불필요**.
- **구현 요지**: `src/session_memory.py` 에 `source=="compact"` 분기 → 무거운 5세션 요약 대신 현재 세션 최근 user 턴(최대 4)으로 hybrid recall(`COMPACT_TOP_K=3`, 동일 raw cosine 게이트) → 관련 메모리만 경량 재주입. 게이트 상수·포맷터는 신규 `src/recall_core.py` 로 single-source-of-truth 화(Layer 4 `memory-recall.py` 와 parity 테스트로 동기). numpy re-exec 부트스트랩은 요약 경로 영향 없이 compact 만 graceful skip.
- **검증**: 신규 18 테스트(compact 14 + parity 4) + 전체 회귀 **617 passed / 0 fail** + 실제 인덱스 e2e 스모크(실 메모리 3+2건 재주입 확인).
- **활성화 남음**: `install.sh` 재실행으로 배포해야 적용(현재 master 커밋만, `~/.claude/hooks/session-memory.py` 미배포).
