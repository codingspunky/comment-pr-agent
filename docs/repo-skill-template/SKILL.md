---
name: repo-conventions
description: Conventions and validation for this repository. Always active when the agent works here.
always: true
metadata:
  validate_cmd: "python -m pip install -e . && python -m pytest -q"
---

# <your repo> conventions

Copy this folder into your repository at `.agent/skills/repo-conventions/`
and edit. Everything here is provided to the agent's plan and edit prompts
whenever it works on this repo.

## About `metadata.validate_cmd`

It is the command the agent runs to validate its edits before committing.

Use `&&`, never `;`. With `;` a failed install is swallowed and the tests run
against a stale environment, so validation reports green for the wrong reason.
`&&` is portable across bash and cmd.exe. Do not redirect output — the agent
captures it and posts it back on failure.

**It is disabled by default.** A repo-supplied command is a shell command that
runs on the agent's host, which turns "can commit to a branch" into "can run
code where the GitHub App private key lives". The operator must opt in with
`AGENT_ALLOW_REPO_VALIDATE_CMD=true`, which is safe when the agent runs on an
ephemeral GitHub Actions runner and questionable when it runs on a long-lived
server. If it is off, the agent falls back to `AGENT_VALIDATE_CMD` or
auto-detection.

## What else belongs here

- Layout: "API routes live in `app/routers/`, one file per resource;
  business logic goes in `app/services/`, never in routers."
- Style: "We use ruff with the config in pyproject; line length 100."
- Testing: "Every new endpoint needs a test in `tests/api/` using the
  `client` fixture."
- Don'ts: "Never edit `migrations/` by hand; never touch `legacy/`."

You can also override the agent's builtin skills: a skill here named
`pr-conventions` replaces the builtin one, so your team's commit/PR format
wins.

## Note on trust

Skill bodies are provided to the model as reference material inside the user
turn, not as system instructions. The agent will follow them where they do not
conflict with its own instructions, and they can never influence webhook auth,
comment authorization, routing, or git operations.
