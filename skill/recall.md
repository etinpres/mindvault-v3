---
description: 과거 Claude Code 세션(FTS5+Gemma 재순위) + memory/*.md(hybrid RRF)를 자연어로 검색해 관련 결과를 현재 대화에 주입 (MindVault v3 Layer 2+4)
argument-hint: <검색어>
allowed-tools: Bash
---

사용자가 `/recall $ARGUMENTS`를 호출했다. 다음 순서를 반드시 지키라.

1. Bash 도구로 정확히 아래 명령을 실행 (따옴표 포함, --source 기본 both):
   ```
   python3 /Users/yonghaekim/.claude/scripts/mindvault/recall_cli.py "$ARGUMENTS"
   ```

2. 스크립트는 stdout에 한 줄 JSON을 출력한다. 형태:
   ```json
   {
     "query": "...",
     "memory": [
       {"path":"...","name":"...","description":"...","snippet":"...","score":0.95,"source":["vec","fts"]},
       ...
     ],
     "sessions": [
       {"session_id":"...","first_ts":"...","last_ts":"...","turn_count":N,"summary":"...","raw_snippet":"..."},
       ...
     ]
   }
   ```

3. 결과 해석:

   **memory 섹션 (Layer 4 hybrid 회수)**
   - `out["memory"]` 가 비면 이 섹션 출력하지 말 것.
   - 각 항목을 다음 형식으로:
     ```
     ### 📌 {name} (memory · score {score:.2f} · {source.join('+')})
     {description}
     발췌: {snippet}
     ```

   **sessions 섹션 (Layer 2 JSONL FTS5 + Gemma)**
   - `out["sessions"]` 가 비면 이 섹션 출력하지 말 것.
   - 각 항목을 다음 형식으로:
     ```
     ### {first_ts 앞 10자 YYYY-MM-DD} — `{session_id 앞 8자}`
     {summary가 있으면 summary, 없으면 raw_snippet}
     ```

   **양쪽 모두 비면**: `매칭되는 과거 세션·메모리 없음.` 한 줄만 출력.

4. 마지막에 한 줄 안내:
   `전체 대화를 읽고 싶은 세션 id 또는 메모리 path를 알려줘.`

5. 절대 raw JSON을 그대로 보여주지 마라. 스크립트의 에러는 빈 결과로 처리된다.

6. 이 스킬은 read-only다. 파일 수정·커밋·배포 금지. 검색 결과만 정리해 출력.

**고급 사용**: `--source memory` 또는 `--source sessions` 인자가 명시되면 그 영역만 검색 (CLI에 그대로 전달).
