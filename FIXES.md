# v0.3 audit — every issue found and how it was fixed

> **v0.3.1** adds five further fixes; see "v0.3.1 — second audit".
> **v0.4.0** migrates to `qwen/qwen3.6-27b` and closes the authorization gap;
> see "v0.4.0 — qwen3.6-27b migration".
> **v0.5.0** makes it survive a free tier and adds lint, types and token
> accounting; see "v0.5.0" at the end.

Each entry names the failure, the fix, and the test that would catch a
regression. `pytest -q` → **255 passed**, no credentials required.

## Data loss (the ones that could corrupt a reviewer's branch)

1. **Output truncation silently destroyed files.** `submit_changes` carries the
   COMPLETE new contents of each file and `apply_edits` wrote it verbatim, but
   nothing checked `finish_reason`. The read cap admitted 48,000-char files
   against a 4,000-token (~14,000-char) completion budget, and the 413 handler
   *halved* the budget on retry — making truncation likelier exactly when the
   prompt was largest. A cut-off completion became a half-written file on disk.
   *Fix:* `finish_reason == "length"` raises `TruncatedCompletion` and is never
   salvaged; `Settings.__post_init__` clamps `max_file_chars` against
   `max_tokens` so anything readable is re-emittable.
   → `test_truncated_completion_is_rejected`,
     `test_max_file_chars_clamped_to_output_budget`,
     `test_truncated_edit_fails_without_committing`

2. **The retry loop edited from stale state.** `file_contents` was captured once
   and never refreshed, and the tree was never reverted. Attempt N+1 landed on
   top of attempt N's files while the model was shown the pristine originals —
   so it could not see its own last attempt, and any file created but not
   resubmitted survived into the commit.
   *Fix:* new `reset_retry` node records the attempt's diff, then
   `git reset --hard && git clean -fdx`. The diff is fed into the next edit
   prompt.
   → `test_retry_resets_the_tree_and_shows_the_previous_attempt`,
     `test_reset_worktree_discards_tracked_and_untracked`

3. **`git add -A` committed validation byproducts.** Validation runs before the
   commit and installs dependencies (`*.egg-info`, `node_modules`); only the
   target repo's `.gitignore` stood in the way.
   *Fix:* `commit_paths()` stages exactly the paths `apply_edits` reported.
   → `test_commit_paths_ignores_untracked_build_output`,
     `test_commit_stages_only_the_touched_paths`

## Security

4. **`.git/` and `.github/workflows/` were writable by the model.** Containment
   was enforced, a denylist was not. `.git/hooks/pre-commit` executes on the
   agent host at commit time — outside the sandboxed validation step, which was
   the only documented mitigation. `.github/workflows/*` runs in CI with the
   repo's secrets. `.agent/*` let the agent rewrite its own instructions.
   *Fix:* `PROTECTED_PREFIXES` enforced in both `apply_edits` and
   `read_targets`.
   → `test_apply_edits_blocks_protected_paths`, `test_is_protected_path_does_not_over_match`

5. **`.git/config` held a live token and was readable.** The remote URL carried
   `x-access-token:<token>@`, and `read_targets` would happily ship it into a
   prompt, from where it could reach a public PR body.
   *Fix:* two layers — `.git/` is protected, *and* the token is no longer
   written to the tree at all (one-shot `http.extraHeader` per git command).
   → `test_read_targets_blocks_git_directory`, `test_auth_header_never_lands_in_git_config`

6. **An empty webhook secret failed open.** `github_webhook_secret` defaulted to
   `""`, nothing required it, and `verify_signature` HMAC'd with an empty key —
   reproducible by anyone.
   *Fix:* `verify_signature` rejects an empty secret; `webhook.py` calls
   `require()` at import and refuses to boot.
   → `test_signature_rejects_empty_secret`

7. **`setup.sh` shipped a published secret and `serve-webhook.sh` accepted it.**
   `.env.example` contained `GITHUB_WEBHOOK_SECRET=replace-with-a-long-random-string`,
   `setup.sh` copied it verbatim, and the startup grep matched the placeholder.
   Both "protections" passed while the deployment was open.
   *Fix:* `setup.sh` generates a real secret into `.env`; `PLACEHOLDER_VALUES`
   in config treats documented placeholders as unset; `serve-webhook.sh` asks
   the application rather than grepping the file (which also fixes container
   deployments that set real env vars and ship no `.env`).
   → `test_placeholder_values_are_treated_as_unset`

8. **Repo-supplied `validate_cmd` was arbitrary shell on the host.** Justified
   as "no new trust boundary" — true of the repo's blast radius, false of the
   host's, which holds the App private key for *every* installation.
   *Fix:* opt-in via `AGENT_ALLOW_REPO_VALIDATE_CMD` (default off); the shipped
   Actions workflow enables it, because a runner is ephemeral and repo-scoped.
   → `test_repo_validate_cmd_is_opt_in`

9. **Validation output was posted unredacted.** The repo's test suite is
   arbitrary code and may echo its environment; the tail went into a PR comment.
   *Fix:* secrets stripped from the subprocess environment and scrubbed from
   the output.
   → `test_validation_output_is_redacted`

10. **Skill discovery followed a symlink out of the repo.** `apply_edits` and
    `read_targets` resolve-and-contain; discovery did not, so `.agent/skills`
    as a symlink pulled skills — including `validate_cmd` — from anywhere on
    the host.
    *Fix:* resolve and contain.
    → `test_symlinked_skills_dir_escaping_repo_is_ignored`

11. **Skill bodies were injected into the system prompt** while repo file
    contents went in the user turn — giving repo-authored markdown more
    authority than the agent's own instructions.
    *Fix:* provided as reference material in the user turn.

12. **The trigger was a bare substring match.** Quoting someone's `/agent`
    comment, a fenced code block containing it, or the word `/agentic` all
    fired the agent.
    *Fix:* must start a line, outside quotes and fences, with a word boundary.
    → `test_addressed_ignores_quoted_replies`, `test_addressed_ignores_fenced_code_blocks`,
      `test_addressed_requires_word_boundary`

13. **`get-cloudflared.sh` ran an unverified `latest` binary.**
    *Fix:* pinned version plus SHA-256 verification.

14. **No `.gitignore`** while `setup.sh` wrote `.env` and `.venv/` into the
    project. *Fix:* added; `.env` is also chmod 600.

## Correctness

15. **Closed and merged PRs were treated as open.** `pr.state` was fetched and
    ignored, so the agent would push to a merged PR's branch where nobody would
    see it. → `test_closed_pr_is_refused`

16. **`read_targets(max_files=0)` read everything** — `[: max(0, n) or None]`
    turned 0 into "unlimited" — and reported every file as skipped at the same
    time. → `test_read_targets_zero_files_reads_nothing`

17. **The size cap compared bytes to characters**, so a UTF-8-heavy file under
    the cap was rejected. → `test_read_targets_size_cap_counts_characters_not_bytes`

18. **Absolute paths were mishandled.** Introduced and caught during this pass:
    normalising `/etc/passwd` by stripping the leading slash turned an escape
    attempt into a valid in-repo path. Now rejected explicitly, along with
    Windows drive-qualified paths. → `test_absolute_paths_are_rejected_not_relativised`

19. **The repo listing was unsorted and silently truncated twice** (4000 then
    2000). `os.walk` order varies, so the planner saw a different arbitrary
    slice each run with no indication anything was missing.
    *Fix:* sorted, single cap, and the model is told when it was cut.
    → `test_repo_listing_is_sorted_and_flags_truncation`

20. **`metadata.validate_cmd` was coerced, not validated.** A YAML list became
    the string `"['rm', '-rf', '/']"` and went to `shell=True`.
    → `test_validate_cmd_rejects_non_string`

21. **Skill discovery was case-sensitive.** `skill.md` resolved on Windows (dev)
    and vanished on Linux (deploy) with no error.
    → `test_skill_md_matching_is_case_insensitive`

22. **`slugify` passed non-ASCII into branch names.** → `test_slugify_drops_non_ascii`

23. **The template shipped the bug a previous audit had already fixed.**
    `docs/repo-skill-template/SKILL.md` still used
    `"pip install -e . >/dev/null 2>&1; pytest -q"` — with `;` a failed install
    is swallowed and tests run against a stale environment.
    → `test_detected_python_command_uses_and_not_semicolon`

24. **`open_pr` failure lost the pushed branch.** Now reported with the branch
    name so the work is recoverable.

## Operations

25. **Postgres connections leaked.** `get_checkpointer()` ran per job and called
    `__enter__()` with no `__exit__`. *Fix:* built once per process, and the
    graph is compiled once too.

26. **Checkpointing could never resume.** `thread_id` ended in a random uuid,
    contradicting the "idempotent resumes" the docstring claimed. *Fix:* stable
    per (repo, issue) or the delivery id.

27. **Workspaces collided and leaked.** The directory name used a whole-second
    timestamp and `clone` rmtree'd it first, so two runs on one repo in the same
    second deleted each other's tree. Cleanup read `repo_path` off the final
    state, so any exception leaked the clone into `/tmp`.
    *Fix:* uuid-suffixed paths chosen in the runner, cleaned up in `finally`.
    → `test_workspace_paths_do_not_collide`

28. **`/healthz` was sync**, sharing the bounded threadpool with 20-minute agent
    runs — so a few concurrent runs hung the health check and got the container
    restarted mid-run. *Fix:* `async def`.

29. **Dedupe consumed delivery ids before authorization**, so a run that failed
    mid-flight could not be redelivered. *Fix:* recorded only on accept.

30. **No retry on GitHub 429/5xx.** *Fix:* urllib3 `Retry` on the session. Groq
    calls gained transient backoff and a request timeout.

31. **Full clone on every comment.** *Fix:* `--filter=blob:none --no-tags`.

## Verification

32. **`scripts/smoke_llm.py` had no assertions** — it printed "smoke test
    passed" unconditionally, including on an empty plan or empty edit list,
    while the README positioned it as the check for the untestable half.
    *Fix:* it asserts and exits non-zero.

33. **No tests existed for the orchestration** the README claimed had been
    verified. `tests/test_graph.py` is that harness: both routing modes, the
    retry loop, retry exhaustion, truncation, commit hygiene, guard
    integration. `tests/test_git_ops.py` exercises git against a real repo.

34. **Docs contradicted the code.** The README documented a `/agent new-pr`
    keyword grammar that does not exist and described the result as "stacked",
    while the code bases it on the PR's base branch (parallel).
    → `test_new_pr_from_inside_a_pr_targets_that_prs_base`

35. **Dead code and config.** `json_mode`/`AGENT_JSON_MODE` and `bot_login` were
    documented but read nowhere; `get_pull_request_files` was never called;
    `--association` was accepted and ignored. All removed.

36. **A test that only ran on Linux.** `test_symlinked_skills_dir_escaping_repo_is_ignored`
    called `os.symlink`, which Windows refuses without Administrator or
    Developer Mode — so the suite failed on the same class of cross-platform
    divergence this audit fixed in the skill loader (#21).
    *Fix:* the test skips when the privilege is unavailable, uses
    `target_is_directory=True`, and gained a platform-independent companion
    (`test_skills_dir_outside_the_repo_is_ignored`) so the containment
    guarantee is never left wholly untested. The guard itself was always
    correct on Windows; only the fixture needed the privilege.

37. **The pipeline ran silently.** Nothing logged between nodes, and
    `validate` shells out with `capture_output=True` — so a multi-minute
    dependency install looked like a hang with no way to tell which stage was
    running. *Fix:* every node logs `[n/9]` progress; `validate` logs its
    resolved command, elapsed time, pass/fail, and the output tail on failure;
    `discover_skills` says out loud when a repo `validate_cmd` is being
    ignored.

37. **The PR body claimed validation that never happened.** Found on a real
    run: the agent opened a PR reading "Validation: Ran pytest; all tests
    passed." on a repository where no validation command existed and nothing
    was executed. `pr_body` is produced by `propose_changes` in the `edit`
    node, which runs BEFORE `validate` — so the model cannot know the outcome —
    and the builtin `pr-conventions` skill explicitly instructed it to write a
    "Validation: what was run and the result" section. The skill was asking the
    model to fabricate, and the pipeline ordering guaranteed it would. A
    reviewer trusting that line is the worst failure mode this tool has.
    *Fix:* the skill now forbids any Validation/Testing/Test-plan section and
    says why; `validate` records `validation_ran` and `validation_cmd`; and
    `validation_footer()` appends the truth to both the PR body and the success
    comment — naming the command, the retry count, or stating plainly that the
    changes are UNVERIFIED.
    → `test_skipped_validation_is_declared_not_hidden`,
      `test_passing_validation_names_the_command`,
      `test_validation_footer_counts_retries`

## Added

- `Dockerfile` — the isolated, non-root container the README asks for.
- `.github/workflows/comment-pr-agent.yml` — Actions deployment, with the
  comment body passed through `env:` (interpolating it into `run:` is a shell
  injection hole).
- `.github/workflows/ci.yml` — runs the suite on 3.11 and 3.12.
- `--comment-from-env` on the CLI, for the same injection reason.

---

# v0.3.1 — second audit

Five defects found in a follow-up read of v0.3. Each has a regression test in
`tests/test_fixes_v031.py`; all twelve of those tests fail against v0.3.

## Security

1. **A target repo's `.env` was loaded into the agent's environment.**
   `config.py` called `load_dotenv(find_dotenv(usecwd=True))` at import. Under
   the Actions deployment the working directory is `$GITHUB_WORKSPACE` — the
   *target* repository — so a `.env` committed by whoever opened the pull
   request was read. `override=False` protected only the variables the workflow
   sets explicitly (`GITHUB_TOKEN`, `GROQ_API_KEY`,
   `AGENT_ALLOW_REPO_VALIDATE_CMD`); everything else was fair game:

   - `GROQ_BASE_URL` / `GITHUB_API_BASE` → the client sends
     `Authorization: Bearer <live credential>` to a host the repo chose;
   - `AGENT_VALIDATE_CMD` → an arbitrary shell command on the runner, routing
     around the `AGENT_ALLOW_REPO_VALIDATE_CMD` gate entirely, since that gate
     only ever covered repo *skills* and `.agent.yml`.

   **Fix:** `.env` is now loaded only from operator-controlled paths —
   `$AGENT_ENV_FILE` if set, otherwise a `.env` beside the installed `agent`
   package. Never from the cwd. `AGENT_NO_DOTENV=1` disables it. Local and
   webhook deployments are unaffected (and now work from any subdirectory,
   which the cwd walk only did by luck).
   *Test:* `test_dotenv_is_not_searched_from_cwd`.

2. **`python -m agent.run` was shadowable by the target repo.** The Actions step
   ran it with the target repo as the working directory, and `python -m` puts
   the cwd first on `sys.path` — so a repo with its own top-level `agent/`
   package would have run instead. **Fix:** the workflow calls the
   `comment-pr-agent` console script, which resolves through the installed
   distribution.

## Correctness

3. **The transient-retry budget could never be spent.** `_create_forced` ran
   JSON-repair retries, 413 shrinks and 429/5xx backoff inside one
   `for attempt in range(1 + max_json_retries)` loop — three iterations. Three
   consecutive 429s, routine on the Groq free tier, exhausted the loop and
   surfaced as `RuntimeError("exhausted tool-call retries")` rather than the
   actual rate limit; a single 413 silently consumed a JSON repair attempt.
   **Fix:** three independent counters, each still individually bounded.
   *Tests:* `test_transient_retries_do_not_consume_the_json_repair_budget`,
   `test_transient_retries_are_still_bounded`.

4. **A comment documented a safety mechanism that did not exist.** The 413
   handler claimed *"propose_changes passes `allow_shrink=False` via a floor
   equal to its budget"*. There was no such parameter, so a 413 halved
   `max_tokens` on the one call that must emit COMPLETE file bodies — the exact
   truncation the design exists to prevent. It failed safe (via
   `TruncatedCompletion`) but turned a rate-limit blip into "the change was too
   large to produce safely". **Fix:** `allow_shrink` is now real;
   `propose_changes` passes `False` and gets a new `PromptTooLarge` with an
   actionable message instead of a smaller, riskier request.
   *Tests:* `test_413_does_not_shrink_the_budget_when_shrinking_is_disallowed`,
   `test_413_still_shrinks_for_calls_that_can_afford_it`.

5. **`apply_edits` failures crashed the run, and left partial writes.** The
   `edit` node caught `TruncatedCompletion` but not the `ValueError` raised by
   a protected or escaping path, so it propagated out of `graph.invoke` and the
   reviewer got the runner's generic "I hit an internal error" comment with no
   indication of which path was refused. `apply_edits` also validated and wrote
   in a single pass, so a rejected fourth edit left the first three on disk —
   in the Actions checkout, which the runner does not clean between steps.
   **Fix:** `apply_edits` validates every path before writing anything, and the
   `edit` node reports the rejection as a normal failure reply. `plan` gained
   the same treatment plus an `after_plan` router — an errored plan previously
   fell through to `retrieve` with no target files.
   *Tests:* `test_apply_edits_writes_nothing_when_a_later_edit_is_rejected`,
   `test_edit_node_reports_a_rejected_path_instead_of_crashing`,
   `test_plan_failure_routes_to_respond_failure`.

## Still open (not addressed in v0.3.1)

- `POST` is in the `Retry` allowlist in `github_api.py`, so a 502 on a request
  GitHub actually processed can produce a duplicate PR or comment.
- Fork PRs fail confusingly in the Actions path: `gh pr view --json
  headRefName` returns a bare branch name, which `actions/checkout` resolves
  against the *base* repo.
- Non-fast-forward pushes are unhandled.
- Installation tokens are stored in checkpointed graph state.

---

# v0.4.0 — qwen3.6-27b migration

Six changes. Three are the model swap, three are the fixes from the review that
preceded it. Every one has a named test in `tests/test_qwen_migration.py`.

## Model migration: `openai/gpt-oss-120b` → `qwen/qwen3.6-27b`

19. **The per-call token budgets were non-reasoning-model assumptions.**
    `interpret_comment` requested 1000 completion tokens and `plan_change` 2000.
    That is ample when the entire completion is a small JSON tool call. On a
    dual-mode reasoning model the thinking trace is charged against the *same*
    ceiling, and it routinely exceeds 1000 tokens on its own — so the completion
    stopped at the ceiling, the truncation guard correctly refused to salvage it,
    and **every run failed at the first step**. This is the failure you get from
    changing only `AGENT_MODEL`.
    *Fix:* `AGENT_INTERPRET_MAX_TOKENS` (4000) and `AGENT_PLAN_MAX_TOKENS` (6000),
    so the budgets scale with the model instead of being frozen in the call site.
    → `test_interpret_uses_the_configured_budget_not_a_hardcoded_1000`,
      `test_plan_uses_the_configured_budget_not_a_hardcoded_2000`

20. **Nothing knew the model's output ceiling.** `AGENT_MAX_TOKENS` was accepted
    verbatim. 32,000 is valid on gpt-oss-120b (32,766) and a hard 400 on
    qwen3.6-27b (16,384) — discovered at the worst possible moment, mid-run,
    after the clone and the plan.
    *Fix:* `MODEL_LIMITS` in `config.py`, and a clamp in `__post_init__` for
    `max_tokens`, `interpret_max_tokens`, and `plan_max_tokens`. An unknown model
    falls back to a conservative default rather than being un-runnable.
    → `test_max_tokens_is_clamped_to_the_models_output_ceiling`,
      `test_per_call_budgets_are_clamped_too`,
      `test_unknown_model_still_runs_on_a_conservative_default`

21. **Thinking mode competed with the file contents on the one call that cannot
    afford it.** `propose_changes` emits COMPLETE file bodies and passes
    `allow_shrink=False` precisely because a truncated file is the only outcome
    that can overwrite a reviewer's code. Enabling reasoning globally would spend
    that same protected budget on a trace.
    *Fix:* reasoning effort is per call — on for `interpret`/`plan`, `none` for
    the edit call. `max_completion_tokens` replaces the deprecated `max_tokens`
    (only the former is documented to cover reasoning *and* visible output), the
    reserve in `__post_init__` grows if an operator turns edit-call thinking on
    anyway, `reasoning_format=hidden` keeps traces out of `message.content` where
    tool arguments are parsed from, and unrecognised reasoning parameters are
    dropped on `TypeError` instead of killing the run.
    → `test_edit_call_defaults_to_no_thinking`, `test_edit_call_sends_thinking_off`,
      `test_file_char_reserve_grows_when_the_edit_call_thinks`,
      `test_budget_is_sent_as_max_completion_tokens`,
      `test_reasoning_format_is_never_raw_by_default`,
      `test_unknown_parameters_degrade_instead_of_killing_the_run`

## Review fixes

22. **The recommended deployment had single-layer authorization, and a comment
    claiming otherwise.** `comment-pr-agent.yml` stated "agent.commands.
    is_authorized re-checks in Python as defence in depth." It did not: `run.py`
    called neither `is_authorized` nor `is_addressed`, so the job-level `if:` and
    a bash `grep` were the only gates on the Actions path. The two trigger
    implementations had already drifted — the grep matches a trigger inside a
    fenced code block, `is_addressed` does not.
    *Fix:* `--enforce-authorization` and `--author-association` on the CLI,
    passed by the workflow. Both gates now run and both must pass. Off by default
    so a local operator run, which has no association to present, is unaffected.
    → `test_workflow_passes_the_second_authorization_gate`,
      `test_cli_rejects_an_unauthorized_author`,
      `test_cli_rejects_a_comment_that_is_not_addressed`,
      `test_cli_allows_an_authorized_author`,
      `test_local_operator_runs_are_unaffected`

23. **Every PR body reported `Validation: none — UNVERIFIED`.** Not a bug in the
    footer — the footer was telling the truth. `.env.example` shipped
    `AGENT_VALIDATE_CMD=` empty; the repo skill's `validate_cmd` requires
    `AGENT_ALLOW_REPO_VALIDATE_CMD=true`, which only the Actions workflow sets;
    and auto-detection fires only on a root `pyproject.toml` / `setup.py` /
    `package.json`. A target repo with tests but no root packaging file matched
    none of the three, so validation was skipped on every CLI run — which also
    meant the retry loop never had a failure signal to retry against.
    *Fix:* `.env.example` ships `AGENT_VALIDATE_CMD=python -m pytest -q`, and the
    workflow comments say what `AGENT_ALLOW_REPO_VALIDATE_CMD` is actually for.
    → `test_env_example_ships_a_working_validate_command`

24. **Stale documentation.** README claimed 98 tests against 115 actual.
    *Fix:* 138, and `START-HERE.md` matches.

## Still open (unchanged from v0.3.1)

- No exploration loop. The planner sees a flat listing of up to 2000 paths and
  has no `read_file` or search tool, so it cannot *find* code — only choose from
  what the listing shows. This is the ceiling on what the agent can do in a large
  repository, and it is a design change rather than a fix.
- No structured logging, tracing, or token/cost accounting.
- CI runs pytest only; no ruff or mypy.
- Webhook delivery dedupe is per-process, so it is decoration behind more than
  one replica.
- `apply_edits` validates all paths before writing any, but the writes themselves
  are not atomic: an `OSError` on the third file leaves the first two on disk.
  The retry path resets the worktree, so this is bounded in practice.

---

# v0.5.0 — free-tier survival, lint/types, token accounting

Everything here has a named test in `tests/test_free_tier.py`.

25. **The 429 handler gave up ~45 seconds too early.** `_sleep_backoff` capped
    at 8 seconds with 3 retries — about 14 seconds of total patience against a
    rolling 60-second token window. The provider publishes `retry-after` and
    `x-ratelimit-reset-*` and the code ignored both. This is not an edge case on
    a small budget: one run costs more tokens than a free-tier minute allows, so
    a 429 mid-run is the NORMAL path, and the agent was failing runs that simply
    waiting would have completed.
    *Fix:* read the header (seconds, `7.66s`, `500ms`, `1m2s`), cap the wait at
    75s so a malformed value cannot park a run, fall back to exponential backoff
    when absent, and make the budget configurable via
    `AGENT_MAX_TRANSIENT_RETRIES` (default raised 3 → 5).
    → `test_retry_after_seconds_is_used`, `test_retry_after_parses_duration_suffixes`,
      `test_rate_limit_reset_header_is_a_fallback`,
      `test_unparseable_or_absent_header_falls_back_to_backoff`,
      `test_server_wait_is_capped`, `test_transient_retry_budget_is_configurable`

26. **The reasoning-budget design had no measurement behind it.** v0.4.0's
    central claim is that thinking tokens are charged against the same ceiling as
    the answer, which is why the edit call runs with thinking off. That was an
    argument, not a number — `resp.usage` was discarded on every call.
    *Fix:* `agent/usage.py`. Per-call `prompt`/`completion`/`reasoning`/`cached`
    tokens, plus `rate_limit_cost` (prompt + *requested* budget − cached), which
    is what a call actually spends against a TPM limit and is not the same as
    what it used. Scoped with a `ContextVar`, not a module global, because the
    webhook runs jobs as concurrent background tasks in one process. Logged in
    `finally`, because a run that died is exactly the one whose spend you want.
    Cost estimation stays off unless prices are configured — no stale price table
    in source.
    → `test_usage_records_reasoning_tokens`, `test_rate_limit_cost_uses_requested_not_used`,
      `test_cached_tokens_are_excluded_from_rate_limit_cost`,
      `test_malformed_usage_never_breaks_a_run`, `test_summary_totals_across_calls`,
      `test_cost_is_not_estimated_without_configured_prices`,
      `test_runs_do_not_bleed_into_each_other`

27. **Concurrency was grouped per issue, but rate limits are per organisation.**
    Two PRs commented on at once meant two runs draining one token bucket, and
    both failing with 429s. The git race the old grouping solved is real; the
    shared-quota problem is a different one it never addressed.
    *Fix:* group on `${{ github.repository }}`. The per-issue grouping is noted
    as the right choice on a paid tier with headroom.
    → `test_workflow_serialises_runs_repo_wide`

28. **The planner's diff slice was hardcoded at 8000 chars** — roughly 2,300
    tokens, about a third of a small TPM minute, for context it barely uses on a
    two-file repository.
    *Fix:* `AGENT_MAX_DIFF_CHARS`.
    → `test_diff_cap_is_configurable`

29. **No lint, no type checking.** Added, and both now gate CI.
    - mypy found 4 errors, all one pattern in `graph.py`: `pr_number: int | None`
      reaching an `int` parameter. None were live bugs — three were guarded by
      checks mypy cannot narrow through a TypedDict `.get()`, and the fourth was
      safe only because of a guarantee living in `decide_mode` in another file.
      Fixed by binding the value locally so each invariant is provable where it
      is used rather than by cross-file reasoning.
    - ruff selects `E,F,I,B` at `line-length = 100`. `UP` is deliberately off: it
      is a correct but whole-codebase cosmetic diff, and belongs in its own
      commit.
    - Bugbear immediately caught `B017 pytest.raises(Exception)` in v0.4.0's own
      new test — and tightening it to the specific error revealed the test had
      never exercised the path it claimed, failing on an unrelated
      `AttributeError` from an unrealistic test double. That is the whole case
      for the tool: it found a real defect in the change that introduced it.
    - Long tool-schema descriptions were reflowed to satisfy `E501`. The parsed
      schemas were snapshotted before and compared after: **byte-identical**, so
      no prompt text moved.

30. **A `.env.free-tier` profile**, with the per-call token arithmetic in its
    header — the charge before and after, and the honest note that the total is
    still more than one minute's allowance, which is precisely why fix 25 is
    what makes it work. It also declines to copy the provider's suggested
    `presence_penalty=1.5` for non-thinking mode: that is tuned for dialogue, and
    penalising repeated tokens while emitting whole source files fights against
    indentation, imports and keywords.
    → `test_free_tier_profile_disables_thinking`,
      `test_free_tier_profile_survives_the_startup_clamp`,
      `test_free_tier_profile_configures_validation`,
      `test_free_tier_profile_raises_transient_patience`

## Still open

- No exploration loop. The planner sees a flat listing and has no `read_file` or
  search tool, so it cannot *find* code — only choose from what the listing
  shows. On the numbers in the free-tier profile a loop is not affordable here at
  all: it would need a paid tier. This is a design change, not a fix.
- `apply_edits` validates all paths before writing any, but the writes are not
  atomic; an `OSError` on the third file leaves the first two on disk. Bounded in
  practice by the worktree reset on retry.
- Webhook delivery dedupe is per-process. The real fix is external (Redis, SQS
  FIFO, a unique constraint), not a patch.
- Prompts are not structured for cache hits. Cached prefixes do not count toward
  TPM, and `cached_tokens` is now logged — if it stays at zero, reordering the
  prompt so invariant content comes first is free headroom.
- `MODEL_LIMITS` is a hardcoded table of provider figures and will go stale.
- `AGENT_TRIGGER` is honoured everywhere in Python but hardcoded as `/agent` in
  the workflow's bash gate; a custom trigger breaks the Actions path (loudly,
  since v0.4.0 re-checks in Python).

---

# v0.5.1 — applying a profile without losing your credentials

31. **`cp .env.free-tier .env` destroys the config it is meant to adjust.** The
    profiles ship their secret keys empty — that is correct for a template and
    wrong for a copy target. Overwriting a working `.env` blanks `GROQ_API_KEY`
    and the App settings, and nothing says so: the next run just reports missing
    settings, or picks up a placeholder.
    *Fix:* `apply-profile.sh` / `scripts/apply_profile.py`, a merge rather than a
    copy. An empty profile value never overwrites an existing one; the keys in
    `NEVER_OVERWRITE` are kept even if a profile does supply a value, because a
    profile has no business asserting where your private key lives; settings the
    profile does not mention are untouched; new keys are appended, so an `.env`
    written before v0.5.0 still picks up the reasoning settings; comments and
    ordering survive; and a timestamped backup is written first. Idempotent, and
    reversible with `apply-profile.sh example`.
    → `test_existing_secrets_are_never_blanked`,
      `test_a_profile_cannot_overwrite_a_credential_even_if_it_sets_one`,
      `test_a_credential_you_do_not_have_yet_is_still_offered`,
      `test_keys_absent_from_the_profile_are_untouched`,
      `test_new_keys_are_appended`, `test_comments_and_ordering_survive`,
      `test_merge_is_idempotent`,
      `test_applying_the_free_tier_profile_to_a_filled_env_keeps_the_key`,
      `test_free_tier_profile_can_be_reversed_with_the_example_profile`

32. **Both profiles shipped a non-empty `GITHUB_APP_PRIVATE_KEY_PATH`.** Found by
    the test written for fix 31 on its first run. The `NEVER_OVERWRITE` guard
    already handled it, so it was not a live bug — but a default path pointing at
    `/run/secrets/` exists on no developer machine, and failing later with "no
    GitHub credentials" is less clear than failing immediately on an unset one.
    *Fix:* both profiles ship it empty, with the container path as a comment.
    → `test_shipped_profiles_leave_every_secret_empty`

33. **The bad-profile error offered your own backups as profiles.** The filter
    used `endswith(".backup")`, but backups are named `.env.backup.<timestamp>`.
    *Fix:* substring match, and `.env.backup.*` added to `.gitignore`.

---

# v0.6.0 — validation that works on a repository nobody configured

34. **`AGENT_VALIDATE_CMD` is global, and v0.5.0 shipped it pinned.** That was
    the wrong fix for "Validation: none". The setting belongs to the agent
    installation, not to a repository, so a command tuned for one repo is applied
    to every repo — point the agent at a second project and validation fails for
    reasons that have nothing to do with the edit. v0.5.0 traded *no* validation
    for *wrong* validation on every repo but one.
    *Fix:* both profiles ship it empty. Detection carries the general case.
    → `test_env_example_does_not_pin_a_repo_specific_validate_command`,
      `test_free_tier_profile_leaves_validation_to_detection`

35. **Detection missed `requirements.txt` entirely** — probably the most common
    way a Python repo declares dependencies. It recognised only `pyproject.toml`,
    `setup.py` and `package.json`.
    → `test_requirements_txt_is_detected`

36. **Detection only looked at the repository root.** A repo keeping its project
    one directory down matched nothing and silently fell through to "skipped" —
    which is how a real target repo ended up needing a hand-written command.
    *Fix:* root first, then immediate subdirectories, sorted so the choice is
    deterministic (`os.listdir` order is arbitrary; an unsorted search picks a
    different directory between runs on the same repo). Vendored trees are
    skipped so a dependency's own `package.json` is never mistaken for the
    repo's suite, and the command runs in the directory holding its manifest.
    → `test_a_project_one_level_down_is_detected`, `test_root_wins_over_a_subdirectory`,
      `test_vendored_manifests_are_ignored`, `test_subdirectory_choice_is_deterministic`

37. **New ecosystems:** Go (`go test ./...`), Rust (`cargo test`), and a
    `Makefile` with a `test:` target. Make is deliberately last — such a target
    usually delegates to whichever real tool is present, and calling that tool
    directly gives better failure output. `.PHONY: test` and an indented `test:`
    recipe line are correctly not treated as targets. Node picks `npm ci` only
    when a lockfile exists, because `npm ci` fails outright without one.
    → `test_go_and_rust_need_no_install_step`, `test_makefile_is_a_last_resort`,
      `test_a_phony_declaration_is_not_a_test_target`, `test_npm_ci_only_with_a_lockfile`

38. **Detected Python validation ran `pip install` into the agent's own
    environment.** Two problems. A target repo's pins could collide with `groq`
    or `langgraph` and break the agent itself. And a repo importing something the
    agent happened to have installed would pass validation *without declaring
    it* — a false green, the exact failure `validation_footer` exists to prevent.
    *Fix:* a throwaway virtualenv, deliberately WITHOUT `--system-site-packages`
    so the false green is caught, created outside the worktree (`reset_worktree`
    runs `git clean -fdx`, which deletes ignored files, so a venv inside the repo
    would vanish mid-run), cached across retries, destroyed in the runner's
    `finally`. Falls back to the ambient environment if creation fails, rather
    than failing a run over an optional hardening measure.
    → `test_isolated_validation_uses_a_venv_outside_the_worktree`,
      `test_venv_is_reused_across_retries`,
      `test_cleanup_removes_the_venv_and_is_idempotent`,
      `test_detected_python_command_installs_pytest`

39. **`apply-profile.sh` could not clear a setting.** Its rule that an empty
    profile value never overwrites was written to protect credentials, but it
    also made clearing impossible — so an existing `.env` with a pinned
    `AGENT_VALIDATE_CMD` would stay pinned forever, defeating fix 34 for exactly
    the installs that needed it.
    *Fix:* the rule now applies only to credentials. For ordinary settings an
    empty profile value is an instruction, not an omission.
    → `test_an_empty_profile_value_clears_an_ordinary_setting`,
      `test_clearing_never_applies_to_a_credential`,
      `test_an_empty_profile_key_you_do_not_have_is_not_added_as_a_blank`

## Still open

- No exploration loop. The planner sees a flat listing and has no `read_file` or
  search tool. On a first real run the model tried to call a `search_codebase`
  tool that does not exist — it asked for the capability by name. The salvage
  path handled it (corrective feedback, bounded retries) but the underlying gap
  is a design change, not a fix, and on free-tier token budgets a loop is not
  affordable at all.
- Detection's long tail: integration tests needing services, monorepos with
  several suites, unusual layouts.
- `pip install -e .` runs the target repo's build backend. The virtualenv bounds
  it; it does not eliminate it.
- Prompts are not structured for cache hits; `cached_tokens` is logged and has
  been zero so far.
- `AGENT_TRIGGER` is honoured in Python but hardcoded as `/agent` in the
  workflow's bash gate.
- `MODEL_LIMITS` is a hardcoded table of provider figures and will go stale.


# v0.6.1 — a no-op is an outcome, not a crash

Found by running v0.6.0 against a real repository three times.

**1. "Already done" killed the process.** Asked to add a docstring that was
already present, the model returned zero edits and an explanation — the correct
answer. `propose_changes` raised a bare `RuntimeError`, nothing caught it, and
the run ended in a stack trace printed *twice*.

`NoChangesNeeded` is now a distinct exception (subclassing `RuntimeError`, so
older callers still work), raised only when the model made a deliberate,
explained decision. `no_change_reason()` is the pure predicate: it trusts the
new `no_changes_needed` schema field, and falls back to narrowly-matched prose
**only** when `edits` is an explicitly empty list. A malformed response that
happens to contain the words "no changes needed" is still a failure — a model
that fails to produce edits will happily write confident prose, and promoting
that to a decision would silently swallow real breakage.

The graph gained a terminal `respond_no_changes` node. It posts an explanation
and writes no validation footer, because nothing was validated.

**2. The CLI had no exception handler.** `run.py` called `run_agent` bare, so
anything the graph did not convert into an in-state error escaped as a
traceback instead of the documented `FAILED: <reason>`. Now every exit is one
line and a distinct status code: `OK:` / `NO CHANGES:` (0), `FAILED:` (1),
`interrupted` (130). `runner.py` moved its traceback to DEBUG; `--debug` brings
it back.

**3. The PR footer leaked local paths.** It printed the absolute isolated-venv
interpreter — `"C:\Users\<name>\AppData\Local\Temp\agent-validate-xxxx\
Scripts\python.exe"` — three times, into a public repository. `short_command()`
reduces it to `python` for **display only**; what executes keeps its absolute
paths, since rewriting those would undo the isolation. Probing it found a URL
false positive (`https://example.com/python-tools.whl` → `httppython-tools.whl`)
that the first regex had; the lookbehinds fixing it are tested.

**4. `AGENT_MAX_FILE_CHARS` 4000 → 6000** in the free-tier profile. 4000 is
below the size of an ordinary source file: a 4,053-character module was refused
outright, after paying for a plan call to discover it. The startup clamp allows
7,875 at `AGENT_MAX_TOKENS=3000`, so 6000 keeps headroom.

**5. `setup.sh` lied about `AGENT_VALIDATE_CMD`,** claiming it ships
preconfigured with `python -m pytest -q`. It ships empty — which is what turns
on auto-detection.

**6. `pythonpath = ["."]`.** `tests/test_apply_profile.py` imports
`scripts.apply_profile`, which is not a distributed package, so the bare
`pytest` console script (what `setup.sh` runs) failed collection while
`python -m pytest` passed. A suite whose result depends on how you invoke it is
not a suite you can trust.

`pytest -q` → **232 passed**. ruff and mypy clean.


# v0.6.1a — the Actions workflow, made actually runnable

The workflow shipped in v0.6.0 could not have worked as written. Nothing tested
it, because it is configuration rather than code. `tests/test_workflow.py` now
does; run against the old file, 11 of its 23 assertions fail.

**The install step pointed at a repo that does not exist** —
`github.com/YOUR-ORG/comment-pr-agent`. The source is now a single greppable
`AGENT_PACKAGE` variable, and `publish-agent.sh` creates the repo it refers to.
That script excludes `.env` from what it stages and aborts if one appears
anyway; a committed Groq key is not a recoverable mistake.

**It carried no token budget.** The stock edit call requests 12,000 completion
tokens; Groq charges prompt + requested against the TPM ceiling whether used or
not, so on a free tier that is ~14,100 against 8,000 — not slow, impossible.
The free-tier block from `.env.free-tier` is now inline, with a note to delete
it on a paid tier.

**`AGENT_ALLOW_REPO_VALIDATE_CMD` was `true`, and is now `false`.** It sounds
strictly better and is not: a repo skill's command overrides detection, runs at
the repo root and outside the isolated venv. The aiops skill's command did not
install pytest, and a fresh runner has none, so validation would have failed
with `ModuleNotFoundError` on a change that was fine. Detection installs pytest
itself. (The skill's command was fixed too, for anyone who does set this true.)

**Added a 👀 reaction** on the triggering comment. Without it the reviewer waits
a minute with no feedback and assumes nothing happened.

`GITHUB-ACTIONS.md` is the four-step setup, including the two behaviours that
surprise people: PRs are authored by `github-actions[bot]`, and pushes made with
`GITHUB_TOKEN` do not trigger other workflows.

`pytest -q` → **255 passed**. ruff and mypy clean.
