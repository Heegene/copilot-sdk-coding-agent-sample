# Copilot SDK sample — Coding-agent-ish implementation on GHES Setup Guide

This guide walks through the full process of installing and configuring this Copilot SDK-based coding-agent-ish implementation on a GitHub Enterprise Server.

---

## Prerequisites

| Item | Minimum Requirement | Notes |
|------|---------------------|-------|
| **GHES Version** | 3.16+ | Reusable workflows support required |
| **Self-hosted runner** | Ubuntu 20.04+ or RHEL 8+ | `actions-runner` installed |
| **Python** | 3.11+ | async/await, type hints |
| **Node.js** | 22.x+ | Required for Copilot CLI |
| **GitHub CLI** | 2.x+ | `gh` command |
| **Network** | Copilot API accessible | Runner can reach `api.github.com` |
| **PAT** | GHES PAT + Copilot auth | GHES API token and Copilot auth token are separate |

> **Air-gapped environments**: The Copilot SDK communicates with cloud AI models. It does not work in fully air-gapped networks.

---

## Step 1: Clone/Fork the Central Repository

Clone or fork this repository to your GHES instance.

```bash
# Option A: Fork using gh CLI
gh repo fork ghes-coding-agent --org YOUR_ORG --hostname ghes.example.com

# Option B: Clone and push manually
git clone https://github.com/your-source/ghes-coding-agent.git
cd ghes-coding-agent
git remote set-url origin https://ghes.example.com/YOUR_ORG/ghes-coding-agent.git
git push -u origin main
```

---

## Step 2: Configure Secrets

Set the following secrets in the central repository.

### Repository Secrets

Settings > Secrets and variables > Actions > New repository secret

| Secret Name | Required | Description | Example |
|-------------|----------|-------------|---------|
| `GH_TOKEN` | Yes | GHES PAT (`repo`, `read:org` scope; add `workflow` only when needed) — used for GHES API and git authentication | `github_pat_xxxx` or `ghp_xxxx` |
| `COPILOT_GITHUB_TOKEN` | No | GitHub token for Copilot SDK authentication. If not set, uses the runner's `copilot login` credentials | `github_pat_xxxx` (GitHub.com) |

### Authentication Scenarios

The GHES API and Copilot SDK use **separate credentials**. Choose one of the following two scenarios based on your environment:

#### Scenario 1: Separate Copilot Auth

1. `GH_TOKEN`: PAT for your GHES instance (for GHES API)
2. `COPILOT_GITHUB_TOKEN`: Fine-grained PAT from GitHub.com (requires Copilot access permissions)
3. Secrets: Add both `GH_TOKEN` and `COPILOT_GITHUB_TOKEN`

#### Scenario 2: Pre-authenticated Runner

Pre-authenticate on the self-hosted runner:

1. Run `copilot login` on the runner VM (one-time, interactive OAuth)
2. Add only `GH_TOKEN` as a secret
3. The Copilot SDK/CLI uses the stored OAuth credentials

> `COPILOT_GITHUB_TOKEN` is the **official** environment variable for the Copilot SDK (highest priority).
> The token user must have a **Copilot license** assigned.

### Creating a PAT

1. GHES > Settings > Developer settings > Personal access tokens
2. Create a Classic PAT (`ghp_`) or a Fine-grained PAT (`github_pat_`) if your GHES version supports it
3. Required scopes (for Classic PAT):
   - `repo` (Full control of private repositories)
   - `read:org` (Read org membership)
    - `workflow` (conditional: required when creating or updating `.github/workflows/*` files, such as during deployment. Not required for normal agent runtime)
4. Store the runtime token as `GH_TOKEN` in the target/central repository secrets

> `GH_TOKEN` is not used for Copilot authentication. Copilot authentication uses `COPILOT_GITHUB_TOKEN` or the runner's `copilot login` credentials.

```bash
# Verify the token is valid
export GH_TOKEN="ghp_xxxxxxxxxxxx"
gh auth login --hostname ghes.example.com --with-token <<< "$GH_TOKEN"
gh api --hostname ghes.example.com /user --jq '.login'
```

---

## Step 3: Allow Access to Reusable Workflows

To allow other repositories in the organization to call workflows from the central repository, access must be granted.

### Organization-Level Settings

1. **Organization Settings** > **Actions** > **General**
2. **Actions permissions** > Select "Allow all actions and reusable workflows"
3. Or allow specific repositories only: "Allow select actions and reusable workflows" > Add `YOUR_ORG/ghes-coding-agent`

### Repository-Level Settings

Central repository:

1. **Settings** > **Actions** > **General**
2. Under the **Access** section, select "Accessible from repositories in the organization"

---

## Step 4: Set Up the Self-Hosted Runner

### Automated Setup (Recommended)

```bash
sudo ./scripts/setup-runner.sh
```

This script automatically installs:
- Python 3.11+
- Node.js 22.x
- GitHub CLI (`gh`)
- Copilot CLI
- `uv` (Python package manager)
- Project dependencies

### Manual Setup

<details>
<summary>Manual installation steps (click to expand)</summary>

```bash
# 1. Python 3.11
sudo apt update && sudo apt install -y python3.11 python3.11-venv python3-pip

# 2. Node.js 22
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

# 3. GitHub CLI
sudo apt install -y gh

# 4. Copilot CLI
sudo npm install -g @github/copilot@latest

# 5. uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 6. Dependencies
pip install -r requirements.txt
```
</details>

### Runner Registration

```bash
./config.sh \
    --url https://ghes.example.com/YOUR_ORG/ghes-coding-agent \
    --token RUNNER_REGISTRATION_TOKEN \
    --labels copilot-agent \
    --name "copilot-runner-01"

sudo ./svc.sh install
sudo ./svc.sh start
```

> Adding the `copilot-agent` label to the runner allows workflows to target it with `runs-on: [self-hosted, copilot-agent]`.

---

## Step 4-1: Per-Repository Configuration File

Each target repository can customize agent behavior using a `.github/ghes-agent.yml` file.

```yaml
# .github/ghes-agent.yml (create in the target repository)
default_branch: develop          # PR target branch (default: main)
timeout_minutes: 45              # Agent timeout (default: 30)
max_retries: 5                   # Retry count (default: 3)
output_language: ko              # Agent output language: en | ko (default: en)
branch_prefix: copilot/          # Agent branch prefix
coder_model: claude-sonnet-4.6   # Code generation model
coder_pr_summary_model: gpt-5.4-mini   # Lightweight PR body summary model
reviewer_models:                 # Review model list
  - claude-opus-4.6
  - gpt-5.4
reviewer_summary_model: claude-opus-4.6      # Consensus summary model
reviewer_suggestion_model: claude-opus-4.6   # Inline suggestion formatter model
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_branch` | `string` | `main` | Base branch for PR creation |
| `timeout_minutes` | `int` | `30` | Maximum agent execution time (minutes) |
| `max_retries` | `int` | `3` | Copilot API retry count |
| `output_language` | `string` | `en` | Language for agent-authored review comments, PR bodies, summaries, and inline suggestion `EXPLANATION` text. `en` or `ko`. Code, identifiers, commit messages, file paths, and parser-required markers remain unchanged. |
| `branch_prefix` | `string` | `copilot/` | Prefix for branches created by the agents |
| `coder_model` | `string` | `claude-sonnet-4.6` | Model for Coder, Doc Gen, and CI Fix agents |
| `coder_pr_summary_model` | `string` | `gpt-5.4-mini` | Lightweight model used by CoderAgent to summarize generated PR bodies |
| `reviewer_models` | `list[string]` | `claude-opus-4.6`, `gpt-5.4` | Models run in parallel by ReviewerAgent |
| `reviewer_summary_model` | `string` | first reviewer model | Model that synthesizes multi-model review findings |
| `reviewer_suggestion_model` | `string` | summary model | Model that formats accepted findings as inline Suggested Changes |

> If the file is absent, global defaults apply. You only need to override the fields you want to change.
> `output_language` can also be injected automatically by `./scripts/deploy-to-repo.sh ... --lang ko` (see Step 5).

---

## Step 5: Deploy to Target Repositories

### Automated Deployment (Recommended)

```bash
# Bash (Linux/macOS) — default (caller workflows, English output)
./scripts/deploy-to-repo.sh ghes.example.com YOUR_ORG target-repo "$GH_TOKEN" ghes-coding-agent

# Bash — Korean output + standalone mode (when reusable-workflow cross-repo access is restricted)
./scripts/deploy-to-repo.sh ghes.example.com YOUR_ORG target-repo "$GH_TOKEN" ghes-coding-agent \
    --standalone --lang ko

# PowerShell (Windows)
.\scripts\deploy-to-repo.ps1 -GhesHost ghes.example.com -Owner YOUR_ORG -Repo target-repo -Token $env:GH_TOKEN -Lang ko
```

> The token passed to the deployment script needs the `workflow` scope because the script creates `.github/workflows/*` files in the target repository. After deployment, the target repository's runtime `GH_TOKEN` only needs `repo` for normal issue/PR/git operations. Add `workflow` to that runtime token only if the agent should be allowed to modify workflow files later.

Key options:

| Option | Default | Description |
|--------|---------|-------------|
| `--lang en\|ko` / `-Lang` | `en` | Language for agent-authored review comments, PR bodies, summaries, and inline suggestion `EXPLANATION` text. Writes `output_language` into `.github/ghes-agent.yml`. |
| `--standalone` (bash only) | off | Deploy full standalone workflows instead of caller workflows. Use when reusable-workflow cross-repo access is not configured (recommended for most GHES setups). |

The script:
1. Creates a new branch in the target repository
2. Adds caller (or standalone) workflow files
3. Sets `output_language` in `.github/ghes-agent.yml` to the chosen value
4. Opens a pull request

### Manual Deployment

You can also add caller workflow files directly to the target repository's `.github/workflows/` directory. Example:

```yaml
# .github/workflows/copilot-coder.yml
name: "Copilot Coder Agent"
on:
  issues:
    types: [labeled]

jobs:
  copilot-coder:
    if: |
      github.event_name == 'issues' && github.event.label.name == 'copilot'
    uses: YOUR_ORG/ghes-coding-agent/.github/workflows/copilot-coder-master.yml@main
    with:
      agent_repo: YOUR_ORG/ghes-coding-agent
    secrets:
      GH_TOKEN: ${{ secrets.GH_TOKEN }}
      COPILOT_GITHUB_TOKEN: ${{ secrets.COPILOT_GITHUB_TOKEN }}
```

### Target Repository Secret Configuration

After deployment, add secrets to the target repository as well:

1. **Settings** > **Secrets and variables** > **Actions**
2. Add `GH_TOKEN` (for GHES API and git authentication; `repo` is enough for normal runtime)
3. (Optional) Add `COPILOT_GITHUB_TOKEN` (when the runner does not use `copilot login`)

---

## Step 6: Testing

### First Test: Coder Agent

1. Create a new issue in the target repository:
   - **Title**: "Add hello world endpoint"
   - **Body**: "Create a simple HTTP endpoint that returns 'Hello, World!'"
2. Add the `copilot` label to the issue
3. Verify the workflow execution in the Actions tab
4. Success when the agent creates a PR!

### Second Test: Code Review

1. Open a PR and add the `copilot-review` label
2. Verify the review comments from the two AI models

---

## Troubleshooting

### Quick Checks

```bash
sudo ./svc.sh status
gh api --hostname ghes.example.com /meta --jq '.installed_version'
copilot --version
python3 -c "import agent; print('OK')"
```

### Viewing Logs

```bash
journalctl -u actions.runner.* -f
gh run list --hostname ghes.example.com --repo YOUR_ORG/target-repo --limit 5
gh run view <run-id> --hostname ghes.example.com --repo YOUR_ORG/target-repo --log
```

---

## Related Documentation

- [ARCHITECTURE.en.md](./ARCHITECTURE.en.md) -- System Architecture
- [README.md](../README.md) -- Project Overview
