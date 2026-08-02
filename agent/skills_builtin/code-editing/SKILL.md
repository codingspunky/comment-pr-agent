---
name: code-editing
description: Non-negotiable discipline for producing file edits. Always active.
always: true
---

# Code editing discipline

- Return the COMPLETE new contents of every file you change. Never elide with
  "... rest unchanged ..." — the engine writes your output verbatim to disk.
- Preserve unrelated code exactly: formatting, comments, imports, whitespace.
  The diff a reviewer sees must contain only the requested change.
- Touch the minimal set of files. If the instruction can be satisfied in one
  file, do not refactor three.
- Paths are repository-relative, forward slashes, no leading `./`, and must
  never contain `..`.
- When you create a new file, follow the naming and layout conventions of the
  nearest existing sibling files.
- If the previous attempt failed validation, fix the ROOT CAUSE the output
  points at — do not paper over it by weakening or deleting tests.
