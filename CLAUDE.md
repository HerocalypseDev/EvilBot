# CLAUDE.md

Project: Discord bot. Stack: JavaScript / Node.js.

## Before changing code

- Read the file to change and its callers first.
- Give a one-line plan before editing.
- Fix root cause. Grep every caller of a function before changing it.

## Coding rules (strict, minimal change)

- Change only what was asked. No extra refactors or cleanups.
- No new dependencies without asking first. Prefer stdlib and existing code.
- Reuse existing helpers and patterns in the codebase before writing new ones.
- No abstractions, config, or scaffolding for speculative future needs.
- Match surrounding naming and idiom.
- Minimal comments: only for non-obvious logic. No docstrings on simple functions.
- Logic with branches, loops, parsing, or money/security paths needs one small runnable check.
- Never weaken input validation, error handling, or security checks to make code shorter.

## Destructive and risky actions

- Never delete files or folders without asking first.
- Never read or write `.env`, tokens, or credentials.
- Add dependencies only after asking.

## Git workflow

- Commit and push after each numbered step of a plan, once that step passes its check.
- Write a clear commit message describing the step.
- Do not commit or push partial or failing work.
- Do not rewrite history or force-push.

## Responses

- Short. Lead with the result.
- Report failing tests or errors with the exact output.
- Say plainly when a step was skipped or not verified.
