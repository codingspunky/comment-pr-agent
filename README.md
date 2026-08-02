# comment-pr-agent

A comment-driven pull request agent. A reviewer leaves a comment like
`/agent make the retry backoff exponential`; the agent works on the change,
validates it, commits, and either updates the open PR or opens a new one — then
requests a reviewer and replies on the thread.

Built on **LangGraph** for orchestration, **Groq** for fast LLM inference, and
either **GitHub Actions** (recommended) or a **GitHub App** webhook for auth.

**Skills-driven**: the engine is generic code; the agent's procedural knowledge
(editing discipline, PR conventions, language playbooks, per-repo validation)
lives in versioned `SKILL.md` folders, discovered at runtime with progressive
disclosure — builtin skills ship in `agent/skills_builtin/`, and any target repo
can add or override skills under `.agent/skills/` (template in
`docs/repo-skill-template/`). See `ARCHITECTURE.md` for the design and
`FIXES.md` for the v0.3 audit.

## How it routes (the one decision that matters)

| Trigger location | Comment | What happens |
|---|---|---|
| On an open PR | `/agent <instruction>` | commits to that PR's feature branch (updates the PR in place) |
| Not on a PR (issue / no PR) | `/agent <instruction>` | opens a **new** branch off the default branch and a new PR |
| On an open PR | `/agent <instruction>, in a separate PR` | opens a new PR **parallel** to the one under review, based on that PR's base branch |

Intent is **interpreted by the LLM** (Groq function calling) — there is no
keyword grammar, no regex, no template parsing, and no `new-pr` subcommand. The
`interpret` node reads the comment and returns whether it's a request, whether
you want a separate PR, and the restated task. The pure
`decide_mode(pr_number, wants_new_pr)` function then routes. Everything between
is one shared core: `plan → retrieve → edit → validate (retry) → publish`.

The only string matching anywhere is the addressing gate: the trigger must
start a line, outside quoted text and fenced code blocks.

## Flow

```
comment ──> trigger (Actions job filter, or webhook: verify sig, filter, authorize)
                                                          │
   ingest (auth) ──> interpret (LLM: request? new PR? task) 
        │
   setup workspace (checkout existing PR branch OR new branch off base)
        │
   gather context (sorted repo listing + PR diff)  ──> discover skills
        │
   plan ──> retrieve ──> edit ──> validate ──┐
              ▲                               │ fail & retries left
              └────── reset_retry ────────────┘   (reverts the tree,
        │ pass                                     records the attempt diff)
   commit (touched paths only) + push ──> [new PR? open_pr] ──> request review ──> reply
```

## Deployment A — GitHub Actions (recommended)

Copy `.github/workflows/comment-pr-agent.yml` into the target repo's default
branch and set a `GROQ_API_KEY` repository secret. That's it.

Why this is the default recommendation:

- **No public endpoint**, so no webhook secret to leak, rotate, or forget.
- **`GITHUB_TOKEN` is scoped to one repo and expires with the job.** The
  webhook deployment instead keeps an App private key on a host that also runs
  the target repo's test suite; a compromise there mints tokens for *every*
  installation. On a runner, the blast radius is one repo, one job.
- **The runner is the ephemeral, network-isolated container** the validation
  step needs, instead of one you have to build and maintain.
- **`concurrency:` replaces hand-rolled branch locking.** Two `/agent` comments
  on the same PR queue instead of racing to a non-fast-forward push.

Two behaviours to know about:

- `issue_comment` workflows always run the copy on the **default branch**, so a
  pull request cannot modify the agent's own workflow. Changes to the workflow
  take effect only after merging.
- Commits pushed with `GITHUB_TOKEN` do not trigger most workflows. For
  `pull_request` opened/synchronize/reopened they queue in an
  **approval-required** state — someone with write access clicks "Approve
  workflows to run". If you want the repo's CI to fire automatically on the
  agent's commits, push with a GitHub App installation token
  (`actions/create-github-app-token`) instead.

## Deployment B — webhook service

### 1. Create a GitHub App

Settings → Developer settings → GitHub Apps → New.

- **Webhook URL**: `https://<your-host>/webhook`
- **Webhook secret**: matches `GITHUB_WEBHOOK_SECRET`
- **Permissions (repository)**: Contents = Read & write, Pull requests = Read &
  write, Metadata = Read-only
- **Subscribe to events**: Issue comment, Pull request review comment
- Generate a **private key** (.pem) and install the App on your repos.

### 2. Configure

```bash
cp .env.example .env       # fill in values — the placeholders are rejected
pip install -e .           # add ".[postgres]" for the Postgres checkpointer
```

Verify your key and model before deploying (this exercises the only part not
testable without credentials, and it asserts — it does not just print):

```bash
GROQ_API_KEY=gsk_... python -m scripts.smoke_llm
```

### 3. Run

```bash
docker build -t comment-pr-agent .
docker run --rm -p 8080:8080 --env-file .env --network=... comment-pr-agent
```

The service **refuses to start** without a real `GITHUB_WEBHOOK_SECRET`. The
`.env.example` placeholder counts as absent.

## Run once from the CLI

```bash
python -m agent.run \
  --repo owner/name --issue 42 --pr 42 \
  --installation-id 12345678 \
  --comment "/agent make the retry backoff exponential" \
  --author alice
```

Use `--comment-from-env` (reads `$AGENT_COMMENT`) anywhere the comment body
comes from an untrusted source — interpolating it into a shell command is an
injection hole. The CLI does **not** perform author authorization; it assumes
the operator running it is trusted.

## The model, and why the budget arithmetic matters

Default: **`qwen/qwen3.6-27b`** on Groq — 131,072-token context, **16,384 max
output tokens**, tool use, and dual-mode reasoning.

Two properties of this model drive real configuration, and both of them broke
assumptions that were safe on `openai/gpt-oss-120b`:

**1. The output ceiling, not the context window, is the binding constraint.**
The edit call returns the *complete new contents* of every file it touches, so
the completion budget — not the prompt budget — decides how large a file the
agent can safely rewrite. `Settings.__post_init__` clamps `AGENT_MAX_TOKENS` to
the ceiling in `MODEL_LIMITS` and then clamps `AGENT_MAX_FILE_CHARS` to what
that budget can actually re-emit. A value copied from a larger model degrades
instead of failing with a 400 mid-run.

**2. Thinking tokens are charged against the same ceiling as the answer.** So
the reasoning mode is set per call, not globally:

| Call | Thinking | Why |
|---|---|---|
| `interpret_comment` | on | tiny output (a routing decision), and the judgement is the whole point |
| `plan_change` | on | tiny output (a file list), and file selection is the hardest step |
| `propose_changes` | **off** | output is complete file bodies, and `allow_shrink=False` forbids reducing the budget — a reasoning trace here competes directly with the code |

That third row is the load-bearing one. `propose_changes` is the only call whose
truncation can overwrite a reviewer's file, which is why it refuses to shrink
its budget under a TPM limit; letting a thinking trace eat that same budget
would reintroduce the failure from the other end.

Sampling defaults to `temperature=0.6`, `top_p=0.95` — the provider's
recommendation for *precise coding* in thinking mode. The previous default of
0.1 predates this model: near-greedy sampling makes Qwen's thinking mode repeat
itself, spending the completion budget before the answer arrives.

Switching back is one variable — `AGENT_MODEL=openai/gpt-oss-120b` — and the
clamps re-derive from that model's own limits. Set
`AGENT_REASONING_EFFORT=""` to omit the reasoning parameters entirely for a
gateway that does not implement them; unknown parameters are also dropped
automatically on a `TypeError` rather than killing the run.

## Validation on a repo nobody configured

`AGENT_VALIDATE_CMD` is global to the agent installation. Setting it tunes the
agent to exactly one repository and breaks every other one — so it ships empty,
and detection carries the general case:

| Found | Command |
|---|---|
| `pyproject.toml` / `setup.py` | `pip install -e .` + `pytest -q` |
| `requirements.txt` | `pip install -r …` + `pytest -q` |
| `package.json` | `npm ci` (or `npm install` with no lockfile) + `npm test` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |
| `Makefile` with a `test:` target | `make test` |

Searched at the repository root first, then one directory down — a project kept
in a subdirectory is ordinary, and root-only detection missed it. Vendored trees
(`node_modules/`, `venv/`) are skipped, so a dependency's own `package.json` is
never mistaken for the repo's test suite. The detected command runs in the
directory holding its manifest.

Detection is a heuristic with a long tail it will miss: unusual layouts,
integration tests needing a database, monorepos with several suites. Those are
what the repo-supplied `validate_cmd` and the global override are for. The aim is
*most repos with no configuration, the rest declare it* — not *every repo*.

### Isolation

Detected Python validation installs the target repo's dependencies into a
throwaway virtualenv, created outside the worktree (`git clean -fdx` between
retries would delete one inside it) and destroyed when the run ends. Cached
across retries so the second attempt doesn't rebuild it.

Two reasons, and the second is the important one. A repo's pins would otherwise
collide with the agent's own `groq`/`langgraph`. And more subtly: a repo that
imports something the *agent* happens to have installed would pass validation
without declaring it — a false green, which is the precise failure
`validation_footer` exists to prevent. Set `AGENT_ISOLATED_VALIDATE=false` to
trade that correctness for speed.

Note that `pip install -e .` executes the target repo's build backend either
way. Auto-detection has always run repo-supplied code; the virtualenv bounds
what that code can reach, which is why isolation is on by default rather than
opt-in.

## Running on a free tier

One run of this agent costs more tokens than a small per-minute allowance
permits, so a 429 mid-run is the normal path rather than a fault. Two things
make that survivable, and they only work together:

```bash
bash apply-profile.sh free-tier      # merges into .env, keeps your credentials
```

Use that rather than `cp .env.free-tier .env`. The profiles ship their secret
keys empty, so a straight copy blanks the `GROQ_API_KEY` and App settings you
already filled in — and the failure is silent until the next run reports missing
settings. The script only overwrites budget settings, appends keys your `.env`
predates, leaves your comments and ordering alone, and writes a timestamped
backup first. `bash apply-profile.sh example` reverses it.

That profile turns thinking off and shrinks every budget — see the arithmetic
in the file header, which shows the per-call token cost before and after. And
the agent now reads the provider's `retry-after` header, waiting the amount the
server actually asked for (capped at 75s) instead of the previous fixed 8
seconds against a 60-second window.

Before trusting any published rate-limit figure, read your own — trackers
disagree and limits are per model:

```bash
curl -si https://api.groq.com/openai/v1/chat/completions \
  -H "Authorization: Bearer $GROQ_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen/qwen3.6-27b","messages":[{"role":"user","content":"hi"}],
       "max_completion_tokens":10}' | grep -i "x-ratelimit\|retry-after"
```

## Observability

Every completion logs its token usage, and every run logs a summary:

```
INFO llm call edit: thinking=False prompt=1712 completion=884 reasoning=0 \
     cached=0 requested=3000 tpm_cost=4712
INFO run usage: {'run_id': 'owner/repo#42', 'llm_calls': 3, ...}
```

`reasoning` is the number that matters: the reason `propose_changes` runs with
thinking disabled is that reasoning tokens are charged against the same
completion ceiling as the answer, and this is what turns that argument into a
measurement. `tpm_cost` is prompt + *requested* budget minus cached tokens —
what the call actually spent against a rate limit, which is not the same as what
it used. A `cached` that stays at zero means prompt restructuring is available
free headroom.

Cost estimation is off unless you set `AGENT_PRICE_INPUT_PER_M` /
`AGENT_PRICE_OUTPUT_PER_M`. Prices change, and a stale table in source reports
confident wrong numbers.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

255 tests, no credentials required. They cover the pure logic (parsing, routing,
signature verification, path guards), the skills registry, `git_ops` against a
real repository on disk, the model/token budget arithmetic in
`tests/test_qwen_migration.py`, and — in `tests/test_graph.py` — the full
LangGraph orchestration with GitHub, git, the LLM, and validation faked: both
routing modes, the retry loop, retry exhaustion, truncation handling, and commit
hygiene.

Not covered (needs your credentials): the live Groq call and live GitHub
integration. Run `scripts/smoke_llm.py` and the CLI against a throwaway repo.

## Security posture

- **Protected paths.** The model may never read or write `.git/`,
  `.github/workflows/`, or `.agent/`. Containment alone is insufficient:
  `.git/hooks/*` executes on the agent host at commit time (outside the
  sandboxed validation step), `.github/workflows/*` runs in CI with the repo's
  secrets, and `.agent/*` would let the agent rewrite its own instructions.
- **Credentials never touch the working tree.** Git operations pass the token
  via a one-shot `http.extraHeader`, so nothing is persisted in `.git/config`.
- **Validation output is scrubbed** of known secrets before it is posted to a
  PR comment; the repo's test suite is arbitrary code and may echo its
  environment.
- **Repo-supplied validation commands are opt-in**
  (`AGENT_ALLOW_REPO_VALIDATE_CMD`). Running the repo's own tests is inherent
  to the job; running a shell command the repo *chooses* is a different
  privilege, and on a long-lived host it reaches the App private key.
- **Skill bodies are untrusted input** and are provided as reference material
  in the user turn, not as system instructions. They can never influence
  webhook auth, comment authorization, routing (`decide_mode` stays pure), or
  git/GitHub operations.
- **Authorization** defaults to comment authors with association
  OWNER/MEMBER/COLLABORATOR, plus `AGENT_AUTHOR_ALLOWLIST`. Under Actions there
  are **two** gates: the job-level `if:` stops a runner from starting, and the
  agent step passes `--enforce-authorization`, which re-applies `is_authorized`
  and `is_addressed` in Python. Both must pass. The second layer matters because
  the YAML gate and the workflow's bash trigger grep are separate
  implementations from the webhook's, and they had already drifted — the grep
  matches a trigger inside a fenced code block, `is_addressed` does not.
- **The CLI is a trusted-operator tool by default.** Without
  `--enforce-authorization` it performs no author check, because a local run has
  no association to present. Pass the flag anywhere the comment body comes from
  someone else.

## Production notes

- **Durability.** The webhook uses a FastAPI background task. For real
  workloads, replace `background.add_task(run_agent, job)` with an enqueue to
  SQS/Celery and run the graph in a worker, so a restart doesn't drop in-flight
  runs. `/healthz` is async precisely so long runs can't make it hang and get
  the container restarted mid-run.
- **Dedupe** is per-process, on `X-GitHub-Delivery`, and recorded only once a
  delivery is accepted. Multi-replica deployments need queue-level dedupe.
- **Loop prevention** drops `sender.type == "Bot"` events; keep the App's bot
  identity distinct.
- **Observability.** Wrap the graph nodes with your tracer (e.g. Langfuse) the
  same way you trace other LangGraph apps.
