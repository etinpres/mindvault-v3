---
description: Gemma가 추출한 영구 기억 후보를 검토하고 승인·폐기 (MindVault v3 Sprint 3)
argument-hint: (없음)
allowed-tools: Bash
---

사용자가 `/memory review`를 호출했다. 다음 순서를 반드시 지키라.

1. Bash 도구로 실행:
   ```
   python3 /Users/yonghaekim/.claude/scripts/mindvault/memory_review_cli.py list
   ```

2. stdout에 `{"staged":[{"file":"...","type":"...","title":"...","body":"...","reason":"...","evidence":"...","age_days":N}]}` JSON이 나온다.

3. 결과 해석:
   - `staged`가 비어 있으면: `승인 대기 중인 기억 후보 없음.` 한 줄만 출력하고 끝.
   - 있으면 각 항목을 다음 포맷으로 번호 매겨 보여줘라:
     ```
     ### [{번호}] {title}   *({type}, {age_days}일 전)*
     {body}
     
     > 근거: {evidence}
     > 이유: {reason}
     ```
   - 마지막에 다음 안내:
     `각 항목에 대해 번호와 함께 [y/n/e/s] (승인/폐기/편집/건너뜀) 답해줘. 예: 1y 2n 3s`

4. 사용자가 응답한 뒤:
   - `y` 항목마다: `python3 .../memory_review_cli.py approve {file}` 실행. `{"ok":true}`면 승인 카운트 +1.
   - `n` 항목마다: `python3 .../memory_review_cli.py reject {file}` 실행.
   - `e` 항목: 어느 필드(title/body)를 어떻게 바꿀지 사용자에게 묻고, staged 파일을 직접 Edit 도구로 수정한 뒤 다시 approve 명령 실행.
   - `s` 항목: 아무 동작 없음 (다음 세션에서 재검토).

5. 모든 처리 후 한 줄 요약: `승인 N건, 폐기 M건, 편집 후 승인 E건, 건너뜀 K건.`

6. 절대 raw JSON을 그대로 사용자에게 보여주지 마라. memory/ 실제 파일이나 MEMORY.md를 직접 수정하지 마라 — approve CLI가 유일한 경로다.
