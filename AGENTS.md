# AGENTS.md

Guidance for GitHub Copilot and other coding agents working in this repository.

This file adapts Karpathy-style agent guidelines for this GHES coding-agent project: think
before coding, keep solutions simple, make narrow changes, and verify work against clear goals.

## Working Principles

### 1. Think Before Coding

- Do not silently choose an interpretation when the request is ambiguous.
- State important assumptions before making non-trivial changes.
- Ask a concise clarifying question when uncertainty would change the implementation.
- Surface tradeoffs when there are materially different approaches.
- Push back when a simpler or safer approach better fits the request.

### 2. Keep It Simple

- Implement the smallest solution that genuinely satisfies the user request.
- Do not add speculative features, configuration knobs, or extension points.
- Avoid new abstractions for one-off logic unless the local codebase already uses that pattern.
- Prefer direct, readable code over cleverness.
- If the implementation grows larger than the problem warrants, simplify before finishing.

### 3. Make Surgical Changes

- Touch only files and lines that are necessary for the task.
- Do not refactor adjacent code, rewrite comments, or reformat unrelated areas.
- Preserve existing behavior unless the user explicitly asks to change it.
- Match the style and structure already present in the surrounding module.
- Remove only unused imports, variables, or helpers introduced by the current change.
- Mention unrelated dead code or cleanup opportunities instead of changing them.

### 4. Work Toward Verifiable Goals

- Convert tasks into success criteria before implementing.
- For bug fixes, prefer a failing test or clear reproduction before changing code.
- For new behavior, add focused tests that cover the happy path and meaningful error cases.
- For refactors, verify behavior before and after the change when practical.
- Keep iterating until the relevant checks pass or report exactly what blocked verification.

## Project Context

- This is a Python 3.11+ autonomous coding agent for GitHub Enterprise Server.
- The architecture is async-first: all I/O should use `async`/`await`.
- The project integrates Copilot SDK behavior with GHES API access.
- Core dependencies include `httpx`, `pydantic`, `pydantic-settings`, `structlog`, and
  `pytest-asyncio`.
- Existing project-specific Copilot guidance also lives in `.github/copilot-instructions.md`.

## Python Style

- Add type hints to every function parameter and return value.
- Prefer concrete types over `Any`; use `Protocol` or typed models where appropriate.
- Use built-in generic collections such as `list[str]` and `dict[str, int]`.
- Write Google-style docstrings for public classes and public functions.
- Keep lines at or below 100 characters.
- Format with Black-compatible style and keep imports Ruff/isort friendly.
- Use `structlog` for structured logging; do not add `print()` statements.

## Async and I/O Rules

- Use `async`/`await` for network calls, file operations, subprocess orchestration, and other I/O.
- Do not call async code from synchronous functions with `asyncio.run()` except at an entry point.
- Keep agents stateless; pass execution data through context objects.
- Route GHES API access through `GHESClient`; do not call `httpx` directly from feature code.

## Validation and Security

- Parse and validate external inputs with Pydantic models.
- Never hardcode tokens, API keys, passwords, GHES hosts, or personal credentials.
- Prefer environment variables and `pydantic-settings` for configuration.
- Keep `.env` ignored and do not print or log secret values.
- Sanitize issue bodies, comments, webhook payloads, file paths, and markdown output.
- URL-encode owner, repository, and path segments when constructing GHES API routes.
- Do not hardcode `api.github.com`; support GHES base URLs from configuration.

## Agent Architecture

- Implement each agent as an independent class with `async def execute(self, context: ...)`.
- Agents should not hold mutable execution state between runs.
- Register tools through the Copilot SDK session with clear names and descriptions.
- Keep orchestration, trigger handling, API access, and tool behavior in their existing modules.
- Prefer established local helpers over new infrastructure.

## Testing Expectations

- Use `pytest` and `pytest-asyncio` for tests.
- Name tests as `tests/test_{module}.py`.
- Mock all external API calls; do not make real network requests from tests.
- Add focused tests for new modules and behavior changes.
- Run the narrowest relevant test command first, then broader checks when the blast radius warrants it.
- If a check cannot be run, state the reason and the residual risk.

## Git and Change Hygiene

- Do not commit unless the user explicitly asks.
- Follow Conventional Commit style when preparing commit messages.
- Use `copilot/{issue-number}` branch names when creating issue-driven branches.
- Preserve user changes already present in the working tree.
- Never use destructive git commands such as `git reset --hard` or `git checkout --` unless the user
  explicitly requests that operation.

## Response Style for Copilot

- Be concise and explicit about what changed and how it was verified.
- Lead with risks, bugs, and missing tests when reviewing code.
- Ask only the clarifying questions needed to avoid a wrong implementation.
- Prefer implementing and verifying over giving a long proposal when the request is actionable.
- Keep final summaries short, with clickable file references when useful.

## Definition of Done

A task is done when all of the following are true:

- The change maps directly to the user's request.
- The implementation follows the existing project structure and style.
- Relevant tests, linters, or targeted checks have been run where practical.
- Any unverified behavior, blocked checks, or notable tradeoffs are clearly reported.