# comment-pr-agent — Skills-Driven Architecture

## Design goal

> **The engine is code. The knowledge is skills.**

The engine (LangGraph pipeline, git ops, auth, sandbox) is generic, stable, and
unit-tested. Everything the model needs to *know* — editing discipline, PR
conventions, language-specific procedure, per-repo validation — lives in
**skills**: versioned folders containing a `SKILL.md` with YAML frontmatter,
discovered at runtime and disclosed progressively.

## The skill contract

```
<skill-dir>/
  SKILL.md        # required (matched case-insensitively)
  <resources>     # optional, referenced from the body
```

| Field | Type | Meaning |
|---|---|---|
| `name` | str, required | Unique identifier. Repo skills override builtin skills with the same name. |
| `description` | str, required | One or two sentences. This is ALL the model sees until it loads the skill. Write it as a routing hint. |
| `always` | bool, default false | If true, the full body is provided in every plan/edit prompt. Reserve for small, universally applicable skills. |
| `metadata.validate_cmd` | **str**, optional | Repo skills only, and only honoured when `AGENT_ALLOW_REPO_VALIDATE_CMD=true`. Non-string values are rejected, not coerced. |

Body: markdown procedure, capped at 8,000 characters on load.

## Two tiers, one registry

1. **Builtin skills** — `agent/skills_builtin/`. Owned by the agent team.
2. **Repo skills** — `<repo>/.agent/skills/*/SKILL.md`, discovered after
   checkout. Owned by the repo's team. Same `name` as a builtin **replaces** it.

`SkillRegistry.discover(repo_path)` walks both trees and resolves precedence.
The repo path is **resolved and contained**: `.agent/skills` may be a symlink,
and following one outside the checkout would let a repo pull skills — including
a `validate_cmd` — from anywhere readable on the agent host. `apply_edits` and
`read_targets` guard their paths the same way; discovery is the third
filesystem entry point and is guarded identically.

## Progressive disclosure

1. **Index** (always in the plan prompt): one line per skill.
2. **Always-on bodies**: skills marked `always: true`, provided in full.
3. **On-demand bodies**: the plan call exposes `load_skills` alongside
   `record_plan`. The engine returns the bodies as a tool result and re-invokes,
   forcing `record_plan`. **Bounded to one load round.**

Whatever the planner loaded travels forward in `skill_context` and is provided
to the edit prompt too. The retry loop reuses it without re-loading.

## Where skill content sits in the prompt

Skill bodies are **reference material inside the user turn**, not system
instructions. A repo skill is authored by whoever can commit to the repo, which
is the same trust level as repo file contents — and repo file contents go in
the user turn. Promoting skills to the system role gave repo-authored markdown
more authority than the agent's own instructions, which is backwards.

## Validation command resolution

Order (first hit wins):

1. `metadata.validate_cmd` from a **repo** skill — *only if*
   `AGENT_ALLOW_REPO_VALIDATE_CMD=true`
2. `AGENT_VALIDATE_CMD` env — operator-level default
3. `.agent.yml` `validate:` key — also repo-supplied, same opt-in gate
4. Auto-detection (pyproject → pytest, package.json → npm test)
5. Skip (configurable)

**Why (1) and (3) are now opt-in.** The earlier rationale was that validation
already executes repo code (its tests), so a repo-supplied command adds no new
trust boundary. That is true of the *repository's* blast radius and false of
the *host's*. The host running a webhook deployment holds the GitHub App
private key, which mints installation tokens for every repo the App is
installed on. A repo-chosen shell command therefore escalates "can commit to a
branch in one repo" into "can run code where the credentials for all of them
live". Running the repo's own test suite is inherent to the job; letting the
repo choose an arbitrary command is a separate privilege and is now granted
explicitly. On a GitHub Actions runner — ephemeral, repo-scoped token — turning
it on costs nothing, which is why the shipped workflow does.

**The same trust boundary applies to `.env`.** Configuration is read only from
operator-controlled paths (`$AGENT_ENV_FILE`, or a `.env` beside the installed
package) — never searched for relative to the working directory. Under the
Actions deployment the working directory *is* the target repository, so a
cwd-relative search would let a repo supply `GROQ_BASE_URL`, `GITHUB_API_BASE`
or `AGENT_VALIDATE_CMD`, which is the gate above with the gate removed.

Auto-detected commands use `&&`, never `;`: with `;` a failed dependency
install is swallowed and the tests run against a stale environment, reporting
green for the wrong reason.

## The retry loop

```
edit → validate → (fail, retries left) → reset_retry → edit
```

`reset_retry` does two things:

1. records `git diff` of the failed attempt into `previous_attempt_diff`;
2. `git reset --hard && git clean -fdx`.

Both matter. Without the reset, attempt N+1 is applied on top of attempt N's
files while the model is shown the pristine originals — so a file created by a
discarded attempt survives into the commit, and the validation step's installed
dependencies pile up in the tree. Without the diff, the model cannot see what
it just tried and re-derives blindly from the original file, which is how a
retry loop repeats the same mistake three times.

`file_contents` in state is therefore always the **pristine** tree, matching
what is on disk at the start of every attempt.

## Output truncation

`submit_changes` carries the COMPLETE new contents of each changed file. A
completion that stops because it hit the token ceiling is a half-written file,
and `apply_edits` would write it over the reviewer's code. So:

- `finish_reason == "length"` raises `TruncatedCompletion` and is never
  salvaged;
- `max_file_chars` is clamped at startup against `max_tokens`, so a file that
  passes the read cap can always be re-emitted in full. The earlier defaults
  admitted a 48,000-character file against a ~14,000-character completion
  budget;
- `max_tokens` itself is clamped to the model's published output ceiling
  (`MODEL_LIMITS`), because that ceiling — not the context window — is what
  limits how large a file this agent can rewrite;
- **the edit call does not think.** On a dual-mode model the reasoning trace is
  charged against this same ceiling. Every other call reasons; this one is the
  only call whose truncation can destroy a reviewer's file, and it is already
  forbidden from shrinking its budget, so it does not spend it on a trace
  either. See `Settings.edit_reasoning_effort`.

## Pipeline

```
ingest → interpret → setup_workspace → gather_context → discover_skills
     → plan (index + load_skills tool) → retrieve → edit (skill_context)
     → validate (skill-resolved cmd, retry via reset_retry)
     → commit (touched paths only) / publish / reply
```

## Commit hygiene

Validation runs **before** the commit and installs dependencies (`pip install
-e .` leaves `*.egg-info`, `npm ci` leaves `node_modules`). `git add -A` staged
all of it whenever the target repo's `.gitignore` did not happen to cover it,
so commits go through `git add -A -- <touched paths>` instead — the exact paths
`apply_edits` reported, which also stages deletions correctly.

## What this buys you

- **Per-repo behaviour without redeploys.** Teams tune the agent by committing
  markdown to their own repo.
- **Controlled token spend.** Only descriptions ride in every call.
- **A migration path.** The skill contract is compatible in spirit with
  Anthropic Agent Skills — if the engine is swapped for the Claude Agent SDK,
  the skills move over nearly unchanged. The knowledge outlives the engine.
