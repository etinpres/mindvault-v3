---
description: "[mv3-skill] /close-session 의 짧은 alias (MindVault v3). 본 SKILL 호출 시 즉시 close-session 본문 Read 후 그 단계 그대로 따른다."
argument-hint: [--dry-run]
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

사용자가 `/cs` 를 호출했다. `/close-session` 과 동일.

단일 진실 원천: `~/.claude/commands/close-session.md`. 본 alias 호출 시 메인 Claude 는 즉시 그 본문을 `Read` 한 뒤 거기 단계를 그대로 따른다. 본 alias SKILL 내용이 본 SKILL 과 어긋나면 close-session 본 SKILL 이 우선.

```
Read("~/.claude/commands/close-session.md")
```

이후 그 본문의 §1 (메모리 슬롯 결정) 부터 §9 (요약 보고) 까지 진행.
