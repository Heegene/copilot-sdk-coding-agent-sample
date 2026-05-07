<#
.SYNOPSIS
    Deploy GHES Coding Agent caller workflows to a target repository.

.DESCRIPTION
    This script authenticates with a GHES instance, creates a branch in the
    target repository, adds lightweight caller workflow files, and opens a
    pull request.

.PARAMETER GhesHost
    GHES hostname (e.g. ghes.example.com)

.PARAMETER Owner
    Target repository owner/org

.PARAMETER Repo
    Target repository name

.PARAMETER Token
    Classic PAT with repo and workflow scopes. The workflow scope is required
    because this deployment script creates or updates .github/workflows files.

.PARAMETER CentralRepo
    Name of the central agent repo (default: ghes-coding-agent)

.PARAMETER Lang
    Output language for agent-authored comments, PR bodies, and review messages on
    this repository. Allowed values: en, ko. Default: en.
    Writes 'output_language: <value>' into .github/ghes-agent.yml.

.EXAMPLE
    .\deploy-to-repo.ps1 -GhesHost ghes.example.com -Owner myorg -Repo my-app -Token ghp_xxxx -Lang ko
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GhesHost,

    [Parameter(Mandatory = $true)]
    [string]$Owner,

    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [Parameter(Mandatory = $true)]
    [string]$Token,

    [Parameter(Mandatory = $false)]
    [string]$CentralRepo = "ghes-coding-agent",

    [Parameter(Mandatory = $false)]
    [ValidateSet("en", "ko")]
    [string]$Lang = "en"
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$GhesUrl = "https://$GhesHost"
$ApiBase = "$GhesUrl/api/v3"
$BranchName = "add-copilot-agent-workflows"
$Headers = @{
    "Authorization" = "Bearer $Token"
    "Accept"        = "application/vnd.github.v3+json"
}

function Write-Step { param([string]$Message) Write-Host "`n[*] $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "  ✅ $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "  ⚠️  $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "  ❌ $Message" -ForegroundColor Red }

function Invoke-GhesApi {
    param(
        [string]$Method = "GET",
        [string]$Path,
        [object]$Body = $null
    )
    $uri = "$ApiBase$Path"
    $params = @{
        Uri         = $uri
        Method      = $Method
        Headers     = $Headers
        ContentType = "application/json"
    }
    if ($Body) {
        $params.Body = ($Body | ConvertTo-Json -Depth 10)
    }
    try {
        return Invoke-RestMethod @params
    }
    catch {
        $status = $_.Exception.Response.StatusCode.value__
        throw "API call failed ($Method $Path): HTTP $status - $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "🚀 GHES Coding Agent - Deploy Workflows" -ForegroundColor White
Write-Host "=========================================" -ForegroundColor White
Write-Host ""
Write-Host "  GHES Host:      $GhesHost"
Write-Host "  Target Repo:    $Owner/$Repo"
Write-Host "  Central Repo:   $Owner/$CentralRepo"
Write-Host "  Output Lang:    $Lang"
Write-Host "  Branch:         $BranchName"

# ---------------------------------------------------------------------------
# Step 1: Authenticate
# ---------------------------------------------------------------------------
Write-Step "Step 1/5: Verifying authentication ..."

try {
    $user = Invoke-GhesApi -Path "/user"
    Write-Ok "Authenticated as $($user.login)"
}
catch {
    Write-Err "Authentication failed. Check your token."
    exit 1
}

# ---------------------------------------------------------------------------
# Step 2: Detect default branch
# ---------------------------------------------------------------------------
Write-Step "Step 2/5: Detecting default branch ..."

try {
    $repoInfo = Invoke-GhesApi -Path "/repos/$Owner/$Repo"
    $DefaultBranch = $repoInfo.default_branch
    $ref = Invoke-GhesApi -Path "/repos/$Owner/$Repo/git/ref/heads/$DefaultBranch"
    $BaseSha = $ref.object.sha
    Write-Ok "Default branch: $DefaultBranch ($($BaseSha.Substring(0,7)))"
}
catch {
    Write-Err "Could not detect default branch. Does the repo exist?"
    exit 1
}

# ---------------------------------------------------------------------------
# Step 3: Create branch
# ---------------------------------------------------------------------------
Write-Step "Step 3/5: Creating branch '$BranchName' ..."

try {
    Invoke-GhesApi -Method POST -Path "/repos/$Owner/$Repo/git/refs" -Body @{
        ref = "refs/heads/$BranchName"
        sha = $BaseSha
    } | Out-Null
    Write-Ok "Branch created"
}
catch {
    Write-Warn "Branch may already exist, continuing..."
}

# ---------------------------------------------------------------------------
# Step 4: Create workflow files
# ---------------------------------------------------------------------------
Write-Step "Step 4/5: Creating workflow files ..."

$CentralRepoFull = "$Owner/$CentralRepo"

$CoderWorkflow = @"
name: "Copilot Coder Agent"
on:
  issues:
    types: [labeled]

jobs:
  copilot-coder:
    if: |
      github.event_name == 'issues' && github.event.label.name == 'copilot'
    uses: $CentralRepoFull/.github/workflows/copilot-coder-master.yml@main
    with:
      agent_repo: $CentralRepoFull
    secrets:
        GH_TOKEN: `${{ secrets.GH_TOKEN }}
        COPILOT_GITHUB_TOKEN: `${{ secrets.COPILOT_GITHUB_TOKEN }}
"@

$ReviewerWorkflow = @"
name: "Copilot Code Reviewer"
on:
  pull_request:
    types: [labeled]

jobs:
  copilot-reviewer:
    if: |
      github.event_name == 'pull_request' && github.event.label.name == 'copilot-review'
    uses: $CentralRepoFull/.github/workflows/copilot-reviewer-master.yml@main
    with:
      agent_repo: $CentralRepoFull
    secrets:
        GH_TOKEN: `${{ secrets.GH_TOKEN }}
        COPILOT_GITHUB_TOKEN: `${{ secrets.COPILOT_GITHUB_TOKEN }}
"@

$DocsWorkflow = @"
name: "Copilot Doc Generator"
on:
  issues:
    types: [labeled]
  pull_request:
    types: [labeled]

jobs:
  copilot-docs:
    if: |
      github.event.label.name == 'copilot-docs'
    uses: $CentralRepoFull/.github/workflows/copilot-docs-master.yml@main
    with:
      agent_repo: $CentralRepoFull
    secrets:
        GH_TOKEN: `${{ secrets.GH_TOKEN }}
        COPILOT_GITHUB_TOKEN: `${{ secrets.COPILOT_GITHUB_TOKEN }}
"@

$CiFixWorkflow = @"
name: "CI Fix Agent"
on:
  workflow_run:
    workflows: ['*']
    types: [completed]

jobs:
  ci-fix:
    if: |
      github.event.workflow_run.conclusion == 'failure' &&
      startsWith(github.event.workflow_run.head_branch, 'copilot/')
    uses: $CentralRepoFull/.github/workflows/ci-fix-master.yml@main
    with:
      agent_repo: $CentralRepoFull
    secrets:
        GH_TOKEN: `${{ secrets.GH_TOKEN }}
        COPILOT_GITHUB_TOKEN: `${{ secrets.COPILOT_GITHUB_TOKEN }}
"@

function New-WorkflowFile {
    param(
        [string]$FilePath,
        [string]$Content,
        [string]$CommitMessage
    )

    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Content))

    $body = @{
        message = $CommitMessage
        content = $encoded
        branch  = $BranchName
    }

    # Check if file exists to get SHA for update
    try {
        $existing = Invoke-GhesApi -Path "/repos/$Owner/$Repo/contents/$($FilePath)?ref=$BranchName"
        $body.sha = $existing.sha
    }
    catch {
        # File doesn't exist yet — that's fine
    }

    try {
        Invoke-GhesApi -Method PUT -Path "/repos/$Owner/$Repo/contents/$FilePath" -Body $body | Out-Null
    }
    catch {
        throw "Failed to create $FilePath. The deployment token must include the 'workflow' scope to create or update GitHub Actions workflow files. $_"
    }
    Write-Ok "Created $FilePath"
}

New-WorkflowFile -FilePath ".github/workflows/copilot-coder.yml" `
    -Content $CoderWorkflow `
    -CommitMessage "ci: add Copilot coder agent workflow"

New-WorkflowFile -FilePath ".github/workflows/copilot-reviewer.yml" `
    -Content $ReviewerWorkflow `
    -CommitMessage "ci: add Copilot reviewer agent workflow"

New-WorkflowFile -FilePath ".github/workflows/copilot-docs.yml" `
    -Content $DocsWorkflow `
    -CommitMessage "ci: add Copilot docs agent workflow"

New-WorkflowFile -FilePath ".github/workflows/ci-fix.yml" `
    -Content $CiFixWorkflow `
    -CommitMessage "ci: add CI fix agent workflow"

Write-Ok "All workflow files created"

# ---------------------------------------------------------------------------
# Step 4b: Write output language into .github/ghes-agent.yml
# ---------------------------------------------------------------------------
Write-Step "Writing output language preference (lang=$Lang) to .github/ghes-agent.yml ..."

$AgentYamlPath = ".github/ghes-agent.yml"
$ExistingYaml = $null
$ExistingYamlSha = $null

try {
    $existing = Invoke-GhesApi -Path "/repos/$Owner/$Repo/contents/$($AgentYamlPath)?ref=$BranchName"
    $ExistingYamlSha = $existing.sha
    $ExistingYaml = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($existing.content))
}
catch {
    # File doesn't exist yet -- will create fresh
}

if ($ExistingYaml) {
    # Preserve existing keys; replace or append output_language only.
    $lines = $ExistingYaml -split "`r?`n" | Where-Object { $_ -notmatch '^\s*output_language\s*:' }
    $NewYaml = ($lines -join "`n").TrimEnd("`n") + "`noutput_language: $Lang`n"
    $YamlCommitMsg = "chore: set output_language=$Lang in ghes-agent.yml"
}
else {
    $NewYaml = @"
# ghes-coding-agent per-repository configuration
output_language: $Lang
"@
    $YamlCommitMsg = "chore: add ghes-agent.yml (output_language=$Lang)"
}

$EncodedYaml = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($NewYaml))
$yamlBody = @{
    message = $YamlCommitMsg
    content = $EncodedYaml
    branch  = $BranchName
}
if ($ExistingYamlSha) { $yamlBody.sha = $ExistingYamlSha }

Invoke-GhesApi -Method PUT -Path "/repos/$Owner/$Repo/contents/$AgentYamlPath" -Body $yamlBody | Out-Null
Write-Ok "$AgentYamlPath updated (output_language=$Lang)"

# ---------------------------------------------------------------------------
# Step 5: Create pull request
# ---------------------------------------------------------------------------
Write-Step "Step 5/5: Creating pull request ..."

$PrBody = @"
## 🤖 Copilot Agent Workflows

This PR adds GitHub Actions workflows that integrate with the [GHES Coding Agent]($GhesUrl/$Owner/$CentralRepo).

### Workflows Added

| Workflow | Trigger | Description |
|----------|---------|-------------|
| ``copilot-coder.yml`` | ``copilot`` label | Autonomous code generation |
| ``copilot-reviewer.yml`` | ``copilot-review`` label | Multi-model AI code review |
| ``copilot-docs.yml`` | ``copilot-docs`` label | Auto documentation generation |
| ``ci-fix.yml`` | Failed CI on ``copilot/`` branches | Automatic CI failure fixes |

### Agent Output Language

This repository is configured to receive agent-authored comments, PR bodies, and code-review messages in **``$Lang``** (see ``.github/ghes-agent.yml``). To change it later, edit the ``output_language`` key in that file.

### Setup Required

After merging, add these repository secrets:
- **GH_TOKEN** (required): GHES PAT with ``repo`` scope — for GHES API and git operations. Add ``workflow`` only if the agent should be allowed to modify ``.github/workflows/*`` later.
- **COPILOT_GITHUB_TOKEN** (optional): GitHub token for Copilot SDK auth. If not set, runner ``copilot login`` credentials are used

---
*Deployed by GHES Coding Agent setup script*
"@

try {
    $pr = Invoke-GhesApi -Method POST -Path "/repos/$Owner/$Repo/pulls" -Body @{
        title = "ci: Add Copilot Agent workflows (output: $Lang)"
        body  = $PrBody
        head  = $BranchName
        base  = $DefaultBranch
    }
    $PrUrl = $pr.html_url
}
catch {
    Write-Err "Failed to create pull request: $_"
    exit 1
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=========================================" -ForegroundColor White
Write-Host "🎉 Deployment complete!" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor White
Write-Host ""
Write-Host "  Pull Request: $PrUrl"
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    1. Review and merge the PR above"
Write-Host "    2. Add repository secrets:"
Write-Host "       - GH_TOKEN (required): GHES PAT with 'repo' scope"
Write-Host "         Add 'workflow' only if the agent should modify .github/workflows/* later"
Write-Host "       - COPILOT_GITHUB_TOKEN (optional): GitHub token for Copilot SDK auth"
Write-Host "         If not set, SDK/CLI uses runner 'copilot login' credentials"
Write-Host "    3. Create an issue and add the 'copilot' label to test"
Write-Host ""
Write-Host "  For more details, see: docs/SETUP.md"
Write-Host ""
