"""Live smoke test for the Groq half: interpret -> plan (with skills) -> edit.

Run with a real key:  GROQ_API_KEY=gsk_... python -m scripts.smoke_llm

Exercises the code paths not testable offline, including the progressive
disclosure loop (skill index + load_skills round) and the tool_use_failed
JSON-repair path (implicitly, if the model fumbles escaping).

This script ASSERTS. The previous version printed "smoke test passed"
unconditionally -- it would report success on an empty plan, an empty edit
list, or a model that decided the comment was not a request at all. Exits
non-zero on any failure so CI and humans can trust the result.
"""
from __future__ import annotations

import sys

from agent import llm
from agent.config import get_settings
from agent.skills import SkillRegistry


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def main() -> int:
    settings = get_settings()
    settings.require("groq_api_key")
    print(f"model = {settings.model}")

    comment = ("/agent add a docstring to the greet function — "
               "please do it in a separate PR")

    print("--- interpret_comment() ---")
    intent = llm.interpret_comment(comment, has_open_pr=True, settings=settings)
    print("is_request:", intent.get("is_request"))
    print("wants_new_pr:", intent.get("wants_new_pr"))
    print("instruction:", intent.get("instruction"))
    check(intent.get("is_request") is True,
          "interpret_comment did not recognise an explicit change request")
    check(bool((intent.get("instruction") or "").strip()),
          "interpret_comment returned an empty instruction")
    check(intent.get("wants_new_pr") is True,
          "interpret_comment missed the explicit 'in a separate PR' intent")

    print("--- plan_change() with skill disclosure ---")
    reg = SkillRegistry.discover(repo_path=None)   # builtin skills only
    repo_files = ["README.md", "app/greet.py", "tests/test_greet.py", "pyproject.toml"]
    plan, loaded, context = llm.plan_change(
        intent.get("instruction", ""),
        repo_files,
        diff=None,
        skills_index=reg.render_index(),
        always_skill_context=reg.render_always(),
        load_skill_bodies=reg.load,
        settings=settings,
    )
    print("skills loaded on demand:", loaded or "(none — planned directly)")
    print("target_files:", plan.get("target_files"))
    print("approach:", plan.get("approach"))
    check(llm.valid_plan(plan), f"plan_change returned an invalid plan: {plan}")
    check(all(isinstance(f, str) and not f.startswith("/") for f in plan["target_files"]),
          "plan_change returned absolute paths")

    print("--- propose_changes() ---")
    file_contents = {
        "app/greet.py": 'def greet(name):\n    return f"Hello, {name}!"\n',
    }
    result = llm.propose_changes(
        intent.get("instruction", ""),
        plan.get("approach", ""),
        file_contents,
        skill_context=context or reg.render_always(),
        settings=settings,
    )
    edits = result.get("edits", [])
    print("edits:", [(e.get("path"), e.get("action")) for e in edits])
    print("summary:", result.get("summary"))
    print("pr_title:", result.get("pr_title"))
    check(llm.valid_edits(result), f"propose_changes returned no usable edits: {result}")
    written = [e for e in edits if e.get("action") == "write"]
    check(bool(written), "propose_changes produced no write edits")
    check(all((e.get("content") or "").strip() for e in written),
          "propose_changes produced a write edit with empty content")
    # The edit must still contain the original function -- a truncated or
    # careless rewrite that drops it is exactly the data-loss case we guard.
    greet_edit = next((e for e in written if e.get("path", "").endswith("greet.py")), None)
    if greet_edit:
        check("def greet" in greet_edit.get("content", ""),
              "the rewritten greet.py no longer defines greet()")

    print("\nsmoke test passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeFailure as exc:
        print(f"\nSMOKE TEST FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
