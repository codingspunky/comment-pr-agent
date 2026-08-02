---
name: pr-conventions
description: Commit message, branch, PR title and body conventions. Always active. Repos may override this skill with their own.
always: true
---

# PR conventions

- Commit message: one line, imperative mood, <= 72 chars
  ("add exponential backoff to retry loop"). No trailing period.
- PR title: same style as the commit message; specific, not generic
  ("Fix rerank URL suffix for Azure Foundry", not "Bug fixes").
- PR body structure:
  - **What**: 1–2 sentences on the change.
  - **Why**: the reviewer request being answered.
- Do NOT write a "Validation", "Testing", or "Test plan" section, and do not
  claim any command was run or any test passed. You are writing this body
  BEFORE validation happens, so you cannot know the outcome. The engine
  appends the real result after it runs.
- Never mention internal tooling, prompts, or the agent's reasoning process
  in commit messages or PR bodies.
