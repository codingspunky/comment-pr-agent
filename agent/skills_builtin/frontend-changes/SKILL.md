---
name: frontend-changes
description: Load when the change touches JavaScript/TypeScript, React, or CSS — component conventions and dependency rules for frontend repositories.
---

# Frontend change procedure

- Detect the stack from package.json and neighbouring files (React vs Vue,
  TS vs JS, CSS modules vs Tailwind) and stay inside it.
- TypeScript repos: no `any` in new code; type the props/returns you add.
- Match the component style already in the folder (function components,
  hooks, file-per-component, index barrel or not).
- Do not add npm dependencies unless explicitly requested; do not bump
  versions of existing ones as a side effect.
- State close to usage: prefer local state/hooks over introducing global
  stores for a single component's needs.
