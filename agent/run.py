"""CLI: run the agent once against a single comment.

Useful for the GitHub Actions-native variant and for local testing:

    python -m agent.run \
        --repo owner/name --issue 42 --pr 42 \
        --installation-id 12345678 \
        --comment "/agent make the retry backoff exponential" \
        --author alice

Under GitHub Actions, pass the comment through the environment instead of the
command line:

    AGENT_COMMENT="${{ github.event.comment.body }}"  # via env:, never run:
    python -m agent.run --repo ... --issue ... --comment-from-env

Interpolating a comment body directly into a `run:` block is a shell-injection
hole: a comment containing backticks or $(...) executes on the runner.

Anywhere the comment comes from an untrusted author, add the second gate:

    --enforce-authorization --author-association "$ASSOCIATION"

That re-applies `is_addressed` and `is_authorized` -- the same functions the
webhook uses -- in Python, so the workflow's YAML `if:` gate is not the only
thing standing between a drive-by comment and a push. Omitted by default: a
local operator run has no association to present and is trusted by definition.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .commands import is_addressed, is_authorized, load_allowlist_from_env
from .config import get_settings
from .runner import run_agent


def main(argv=None):
    p = argparse.ArgumentParser(description="Run the comment-driven PR agent once.")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--issue", required=True, type=int,
                   help="issue or PR number the comment lives on")
    p.add_argument("--pr", type=int, default=None,
                   help="PR number if the comment is on an open PR")
    p.add_argument("--installation-id", type=int, default=None,
                   help="GitHub App installation id (omit when GITHUB_TOKEN is set)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--comment", help="the comment body")
    group.add_argument("--comment-from-env", action="store_true",
                       help="read the comment body from $AGENT_COMMENT (injection-safe)")
    p.add_argument("--author", default="cli")
    p.add_argument("--author-association", default=None,
                   help="GitHub author_association of the comment author "
                        "(OWNER / MEMBER / COLLABORATOR / CONTRIBUTOR / NONE). "
                        "Required by --enforce-authorization.")
    p.add_argument("--enforce-authorization", action="store_true",
                   help="apply the same author and trigger checks the webhook "
                        "applies. Set this wherever the comment comes from an "
                        "untrusted party -- notably GitHub Actions, where the "
                        "workflow is triggered by anyone who can comment.")
    p.add_argument("--workspace", default=None,
                   help="use an existing checkout instead of cloning "
                        "(GitHub Actions: $GITHUB_WORKSPACE)")
    p.add_argument("--debug", action="store_true",
                   help="verbose logging, including a traceback when a run fails")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.comment_from_env:
        comment = os.environ.get("AGENT_COMMENT", "")
        if not comment.strip():
            print("AGENT_COMMENT is empty", file=sys.stderr)
            sys.exit(2)
    else:
        comment = args.comment

    # Defence in depth for the Actions deployment. The workflow gates on the
    # author association in its `if:` and greps for the trigger in bash; this
    # re-applies BOTH in Python, against the same functions the webhook uses.
    #
    # Two implementations of a security check drift. They already had: the bash
    # grep matches a trigger inside a fenced code block, `is_addressed` does not.
    # Whichever gate is stricter now wins, because both must pass.
    #
    # Off by default because a local operator run has no association to present
    # and is trusted by definition -- but the workflow passes it explicitly, so
    # the recommended deployment is the one that gets the second layer.
    enforce = args.enforce_authorization or \
        os.environ.get(
            "AGENT_ENFORCE_AUTHORIZATION", "").strip().lower() in {"1", "true", "yes", "on"}
    if enforce:
        settings = get_settings()
        if not is_addressed(comment, settings.trigger_phrase, settings.require_trigger):
            print(f"comment is not addressed to the agent "
                  f"(needs a line starting with {settings.trigger_phrase!r}, "
                  f"outside quotes and code fences)", file=sys.stderr)
            sys.exit(3)
        if not is_authorized(args.author_association, args.author, load_allowlist_from_env()):
            print(f"author {args.author!r} (association "
                  f"{args.author_association or 'none'!r}) is not authorized",
                  file=sys.stderr)
            sys.exit(4)

    job = {
        "repo": args.repo,
        "installation_id": args.installation_id,
        "comment_body": comment,
        "comment_author": args.author,
        "issue_number": args.issue,
        "pr_number": args.pr,
        "workspace": args.workspace,
    }
    # `run_agent` re-raises anything the graph did not convert into an
    # in-state error. Uncaught, that printed a raw traceback as the CLI's final
    # word -- unreadable, and it buried the reason the reviewer needs. Every
    # exit from here is now one line on stderr plus a distinct status code.
    try:
        final = run_agent(job)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # runner.run_agent has already logged this and posted a failure comment.
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        if not args.debug:
            print("re-run with --debug for the traceback", file=sys.stderr)
        sys.exit(1)

    # Neither success nor failure: the model found the work already done. Exit 0
    # -- in CI this is a green build, because nothing is wrong.
    if final.get("no_changes"):
        print(f"NO CHANGES: {final.get('no_changes_reason') or 'nothing to do'}")
        sys.exit(0)
    if final.get("error"):
        print(f"FAILED: {final['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: pr={final.get('pr_url')} commit={final.get('commit_sha')}")


if __name__ == "__main__":
    main()
