# GHES Coding Agent -- Architecture

This document describes the system architecture, component structure, and data flow of the GHES Coding Agent.

---

## System Overview

```
+------------------------------------------------------------------+
|                    GitHub Enterprise Server                        |
|                                                                    |
|  +----------+   label/comment   +------------------+               |
|  |  Issue /  | ---------------->|  GitHub Actions   |               |
|  |    PR     |                  |   (Workflow)      |               |
|  +----------+                   +--------+---------+               |
|                                          |                         |
|                                          v                         |
|                                 +----------------+                 |
|                                 |  Self-Hosted   |                 |
|                                 |    Runner      |                 |
|                                 +--------+-------+                 |
+------------------------------------------+-------------------------+
                                           |
                                           v
                              +---------------------+
                              |    Orchestrator      |
                              |  (agent/orchestrator)|
                              +----------+----------+
                                         |
                        +----------------+----------------+
                        v                                 v
                +--------------+                +--------------+
                | Label Trigger|                |  CI Trigger  |
                |              |                |  (workflow_  |
                | copilot      |                |   run)       |
                | copilot-     |                |              |
                |  review      |                |              |
                | copilot-docs |                |              |
                | copilot-fix  |                |              |
                +------+-------+                +------+-------+
                       |                               |
                       +--------------+----------------+
                                      |
                                      v
                              +------------------+
                              |   Agent Router   |
                              +--------+---------+
                                       |
                  +----------+ +----------+ +----------+ +----------+
                  |  Coder   | | Reviewer | | Doc Gen  | |  CI Fix  |
                  |  Agent   | |  Agent   | |  Agent   | |  Agent   |
                  +----------+ +----------+ +----------+ +----------+
                                       |
                       +---------------+---------------+
                       v               v               v
           +--------------+ +--------------+ +--------------+
           | Copilot SDK  | |  GHES REST   | |    Tools     |
           | / CLI        | |   Client     | |    (git)     |
           +------+-------+ +------+-------+ +--------------+
                  |                |
                  v                v
           +--------------+ +--------------+
           |  Copilot     | |   GHES API   |
           |  Cloud API   | |  (api/v3)    |
           +--------------+ +--------------+
```

---

## Component Descriptions

### Orchestrator (`agent/orchestrator.py`)

The entry point and router of the system.

- Receives GitHub Actions event payloads
- Parses triggers and routes to the appropriate Agent
- Manages agent execution lifecycle (start -> in progress -> complete/error)
- Posts progress comments on Issues/PRs

```python
class Orchestrator:
    """Routes GitHub Actions events to the appropriate agent."""
    async def run(self, event_path: str) -> None: ...
    async def _route_agent(self, ctx: TriggerContext) -> str: ...
```

### Triggers (`agent/triggers/`)

Parses GitHub webhook events and creates a `TriggerContext`.

| Trigger | File | Event |
|---------|------|-------|
| **LabelTrigger** | `label_trigger.py` | `issues.labeled`, `pull_request.labeled` |

#### TriggerContext

The execution context passed to all agents:

```python
@dataclass
class TriggerContext:
    agent_type: AgentType
    event_type: str
    owner: str
    repo: str
    issue_number: int | None
    pr_number: int | None
    issue_title: str
    issue_body: str
    creator: str
    server_url: str
    run_id: str | None
```

### Agents (`agent/agents/`)

Each agent performs an independent unit of work.

#### CoderAgent (`coder_agent.py`)

An agent that analyzes issues, generates code, and opens PRs:

1. Adds an `in-progress` label to the issue
2. Gathers repository context
3. Clones the repository and creates a branch
4. Generates code via Copilot SDK/CLI
5. Commits and pushes the branch
6. Uses a lightweight model to draft PR summary and verification sections from git metadata
7. Creates the PR with a compact change footprint and a pointer to the Files changed tab
8. Updates the completion label

#### ReviewerAgent (`reviewer_agent.py`)

Performs code review by running two AI models in parallel under the same review conditions:

1. Collects the PR diff and changed-file anchors
2. Sends the same diff, changed-file anchors, file context, and shared rubric to Claude and GPT-5.4
3. Both models can use Copilot workspace tools to inspect related callers,
  tests, configuration, docs, and public API boundaries when needed
4. Both models review the full scope; Claude emphasizes security/architecture/maintainability,
   while GPT-5.4 emphasizes correctness/performance/edge cases
5. Posts individual review result comments
6. Generates a consolidated summary with dedupe, agreement/disagreement analysis, and final verdict
7. Generates inline Suggested Changes only for agreed or strongly evidenced findings
  that target valid PR diff lines

#### DocGenAgent (`doc_gen_agent.py`)

Generates and updates documentation.

1. For PRs, collects the changed-file list from the GHES API as seed context
2. For issues, runs as a repository-wide documentation pass
3. Seeds existing key docs such as `README.md`, `docs/README.md`, and `docs/API.md`
4. Starts the Copilot SDK/CLI session in the checked-out working tree
5. Prompts Copilot to use seed files as anchors and inspect related source,
   tests, configuration, and docs with file tools when needed
6. Lets outer orchestration handle commits, pushes, and PR creation after docs
   or docstrings are edited

#### CIFixAgent (`ci_fix_agent.py`)

Analyzes failed CI logs and job metadata to generate automatic fix commits.

### Copilot Session (`agent/copilot_session.py`)

Interface with the Copilot SDK/CLI:

- **SDK mode**: Uses the `github-copilot-sdk` package
- **CLI fallback**: Falls back to the `copilot` CLI when the SDK is not installed
- **Features**: Timeouts, retries, multi-model parallel execution
- **Tool registration**: Supports registering tools for agent use

```python
async with CopilotSessionManager(model="claude-sonnet-4.6") as session:
    result = await session.execute(prompt)
    results = await session.execute_parallel(
        prompt, models=["claude-opus-4.6", "gpt-5.4"]
    )
```

### GHES Client (`agent/ghes_client.py`)

Asynchronous client for the GitHub Enterprise Server REST API:

- Built on `httpx.AsyncClient`
- Automatic retries: 429 + 5xx -> up to 5 attempts
- Bearer token authentication
- Supports both github.com and GHES URL patterns

### Tools (`agent/tools/`)

| Tool | File | Description |
|------|------|-------------|
| **Git Operations** | `git_tools.py` | Branch, commit, push, diff, etc. |

### Config (`agent/config.py`)

```
AppConfig
+-- GHESConfig
+-- CopilotConfig
+-- AgentConfig
```

### Prompt Templates (`agent/utils/prompts.py`)

A Jinja2-based prompt template system.

---

## Data Flow Diagrams

### Issue -> Agent -> PR (Coder Agent)

```
 User                   GHES                  Runner               Copilot Cloud
   |                     |                      |                       |
   |  1. Create Issue    |                      |                       |
   |  + copilot label    |                      |                       |
   | ------------------>|                      |                       |
   |                     |  2. Webhook fires    |                       |
   |                     | ------------------> |                       |
   |                     |                      |  3. Parse event       |
   |                     |                      |     Match trigger     |
   |                     |                      |                       |
   |                     |<---- 4. "Working..." |                       |
   |                     |         comment      |                       |
   |                     |                      |                       |
   |                     |<---- 5. Collect repo |                       |
   |                     |         context      |                       |
   |                     |                      |                       |
   |                     |                      |  6. Code generation   |
   |                     |                      |         request       |
   |                     |                      | --------------------->|
   |                     |                      |                       |
   |                     |                      |<- 7. Generated code   |
   |                     |                      |                       |
   |                     |<---- 8. Branch push  |                       |
   |                     |                      |                       |
   |                     |<---- 9. Create PR    |                       |
   |                     |                      |                       |
   |<--- 10. PR notif.--|                      |                       |
```

### Multi-Model Review (Reviewer Agent)

```
                          +-----------------+
                          |   PR + Diff     |
                          +--------+--------+
                                   |
                          +--------v--------+
                          |  ReviewerAgent  |
                          +--------+--------+
                                   |
                    +--------------+--------------+
                    |                              |
           +--------v--------+           +--------v--------+
           |  Claude Session |           | GPT-5.4 Session |
           |                 |           |                  |
           | Security        |           | Bugs             |
           | Architecture    |           | Performance      |
           | Design          |           | Edge Cases       |
           | Maintain.       |           | Error Hdl.       |
           +--------+--------+           +--------+--------+
                    |                              |
                    +--------------+---------------+
                                   |
                          +--------v--------+
                          |  Consolidated   |
                          |    Summary      |
                          |                 |
                          | Consensus       |
                          | Key Findings    |
                          | Final Verdict   |
                          +--------+--------+
                                   |
                          +--------v--------+
                          | Inline Suggested|
                          |    Changes      |
                          | EXPLANATION text|
                          +-----------------+
```

---

## Security Considerations

### Authentication

- Two credential schemes: GHES API and Copilot SDK may use separate tokens
- `GH_TOKEN` is only for GHES API and git authentication
- `COPILOT_GITHUB_TOKEN` is optional for Copilot SDK authentication; if absent, use runner credentials from `copilot login`
- Tokens are injected only via environment variables or secrets

### Input Validation

- All user input is validated with pydantic
- User input must not be used directly in file paths
- Injection is prevented in Markdown output

### Network Security

- GHES API: HTTPS only
- Copilot API: TLS 1.2+
- Runner outbound network policy must allow access to the GHES API and Copilot API endpoints.

### Code Execution Isolation

- Agent-generated code runs only within the runner VM
- Production secrets are never exposed to the runner

---

## Scalability and Concurrency

### Concurrency Control

#### 1. Copilot Session Semaphore

```python
MAX_CONCURRENT_SESSIONS = 5
_copilot_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
```

#### 2. GitHub Actions Concurrency Groups

```yaml
concurrency:
  group: copilot-coder-${{ github.event.issue.number }}
  cancel-in-progress: true
```

| Workflow | Concurrency Group Key | cancel-in-progress |
|----------|----------------------|-------------------|
| Coder | `copilot-coder-{issue_number}` | `true` |
| Reviewer | `copilot-reviewer-{pr_number}` | `true` |
| CI Fix | `ci-fix-{workflow_run_id}` | `true` |

### Queuing / Backpressure

Uses Actions built-in queuing, runner queue, and rate limit retry mechanisms.

### Failure Domains

```
Agent execution failure
    |
    +-- 1. Caught by except Exception
    +-- 2. Error comment posted
    +-- 3. Process exits (exit code 1)
```

### Per-Repository Configuration

```yaml
# .github/ghes-agent.yml
default_branch: develop
timeout_minutes: 45
max_retries: 5
output_language: ko
branch_prefix: copilot/
coder_model: claude-sonnet-4.6
coder_pr_summary_model: gpt-5.4-mini
reviewer_models:
  - claude-opus-4.6
  - gpt-5.4
reviewer_summary_model: claude-opus-4.6
reviewer_suggestion_model: claude-opus-4.6
```

- Orchestrator reads `.github/ghes-agent.yml` from the target repository at runtime
- If the file is absent, global defaults from `agent/config.py` are used
- Repositories can independently override branch strategy, timeout, output language, and models

---

## Extension Points

### Adding a New Agent

1. Create a new agent class in `agent/agents/`
2. Add a new type to the `AgentType` enum
3. Add routing in `Orchestrator._route_agent()`
4. Add a trigger label in `LabelTrigger.LABEL_MAP`

### Adding a New Tool

1. Create a tool module in `agent/tools/`
2. Register it with the Copilot session

### Customizing Prompts

```python
pm = PromptManager()
pm.register("custom_review", "Your custom prompt template {{ variable }}")
```

## Related Documentation

- [SETUP.en.md](./SETUP.en.md) -- Setup Guide
- [README.en.md](./README.en.md) -- Project Overview
