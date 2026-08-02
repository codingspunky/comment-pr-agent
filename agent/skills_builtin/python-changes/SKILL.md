---
name: python-changes
description: Load when the change touches Python code — module layout, imports, typing, and test expectations for Python repositories.
---

# Python change procedure

- Match the repo's existing style before your own preferences: check whether
  sibling modules use `from __future__ import annotations`, dataclasses vs
  pydantic, absolute vs relative imports — and do the same.
- New public functions get type hints and a one-line docstring. Do not add
  docstrings to code you are not otherwise changing.
- If the repo has tests near the code you changed (tests/ or test_*.py),
  extend them to cover the new behaviour in the same style. A behaviour
  change without a test change is usually an incomplete edit.
- Keep imports at the top of the file, grouped stdlib / third-party / local,
  unless the file already violates this (then match the file).
- Never introduce a new third-party dependency unless the instruction
  explicitly asks for it; prefer the stdlib.
