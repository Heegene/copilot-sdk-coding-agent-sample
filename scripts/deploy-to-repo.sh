#!/bin/bash
# Deploy GHES Coding Agent caller workflows to a target repository
#
# Usage:
#   ./deploy-to-repo.sh <ghes-host> <owner> <repo> <token> [central-repo-name]
#
# Example:
#   ./deploy-to-repo.sh ghes.example.com myorg my-app ghp_xxxx ghes-coding-agent
#
# This script:
#   1. Authenticates with the GHES instance via gh CLI
#   2. Creates a new branch in the target repository
#   3. Adds lightweight caller workflow files
#   4. Opens a pull request with the workflow files

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}ℹ️  $*${NC}"; }
success() { echo -e "${GREEN}✅ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠️  $*${NC}"; }
error()   { echo -e "${RED}❌ $*${NC}" >&2; }
fatal()   { error "$@"; exit 1; }

usage() {
    echo "Usage: $0 <ghes-host> <owner> <repo> <token> [central-repo-name] [--standalone] [--lang en|ko]"
    echo ""
    echo "Arguments:"
    echo "  ghes-host          GHES hostname (e.g. ghes.example.com)"
    echo "  owner              Target repository owner/org"
    echo "  repo               Target repository name"
    echo "  token              Classic PAT with repo and workflow scopes"
    echo "  central-repo-name  Name of the central agent repo (default: ghes-coding-agent)"
    echo ""
    echo "Options:"
    echo "  --standalone       Deploy full standalone workflows instead of caller workflows."
    echo "                     Use this if reusable workflow cross-repo access is not configured."
    echo "                     (Recommended for most GHES setups)"
    echo "  --lang en|ko       Output language for agent-authored comments, PR bodies, and"
    echo "                     review messages on this repository. Default: en."
    echo "                     Writes 'output_language: <value>' into .github/ghes-agent.yml."
    echo ""
    echo "Examples:"
    echo "  $0 ghes.example.com myorg my-app ghp_xxxx                                # Caller mode, English"
    echo "  $0 ghes.example.com myorg my-app ghp_xxxx --standalone --lang ko         # Standalone, Korean"
    exit 1
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
STANDALONE=false
LANG_CHOICE="en"

POSITIONAL_ARGS=()
i=1
while [ $i -le $# ]; do
    arg="${!i}"
    case "$arg" in
        --standalone)
            STANDALONE=true
            ;;
        --lang)
            i=$((i + 1))
            if [ $i -gt $# ]; then
                fatal "--lang requires a value (en|ko)"
            fi
            LANG_CHOICE="${!i}"
            ;;
        --lang=*)
            LANG_CHOICE="${arg#--lang=}"
            ;;
        *)
            POSITIONAL_ARGS+=("$arg")
            ;;
    esac
    i=$((i + 1))
done

case "$LANG_CHOICE" in
    en|ko) ;;
    *) fatal "Invalid --lang value '${LANG_CHOICE}'. Allowed: en, ko" ;;
esac

if [ ${#POSITIONAL_ARGS[@]} -lt 4 ]; then
    usage
fi

GHES_HOST="${POSITIONAL_ARGS[0]}"
OWNER="${POSITIONAL_ARGS[1]}"
REPO="${POSITIONAL_ARGS[2]}"
TOKEN="${POSITIONAL_ARGS[3]}"
CENTRAL_REPO="${POSITIONAL_ARGS[4]:-ghes-coding-agent}"

GHES_URL="https://${GHES_HOST}"
API_BASE="${GHES_URL}/api/v3"
BRANCH_NAME="add-copilot-agent-workflows"
DEFAULT_BRANCH="main"

echo ""
echo "🚀 GHES Coding Agent - Deploy Workflows"
echo "========================================="
echo ""
echo "  GHES Host:      ${GHES_HOST}"
echo "  Target Repo:    ${OWNER}/${REPO}"
echo "  Central Repo:   ${OWNER}/${CENTRAL_REPO}"
echo "  Mode:           $([ "$STANDALONE" = true ] && echo "Standalone (full workflows)" || echo "Caller (reusable workflows)")"
echo "  Output Lang:    ${LANG_CHOICE}"
echo "  Branch:         ${BRANCH_NAME}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Authenticate with gh CLI
# ---------------------------------------------------------------------------
info "Step 1/6: Authenticating with GHES ..."

echo "${TOKEN}" | gh auth login --hostname "${GHES_HOST}" --with-token 2>/dev/null \
    || fatal "Failed to authenticate with ${GHES_HOST}. Check your token."

GH_USER=$(gh api --hostname "${GHES_HOST}" /user --jq '.login' 2>/dev/null) \
    || fatal "Could not fetch authenticated user. Token may lack permissions."

success "Authenticated as ${GH_USER}"

# ---------------------------------------------------------------------------
# Step 2: Create labels
# ---------------------------------------------------------------------------
info "Step 2/6: Creating labels ..."

create_label() {
    local name="$1"
    local color="$2"
    local description="$3"

    gh api --hostname "${GHES_HOST}" \
        "/repos/${OWNER}/${REPO}/labels" \
        -f "name=${name}" \
        -f "color=${color}" \
        -f "description=${description}" \
        --silent 2>/dev/null \
        || true  # Label may already exist
}

create_label "copilot"          "7057ff" "Trigger Copilot Coder Agent"
create_label "copilot-review"   "0e8a16" "Trigger Copilot Code Review"
create_label "copilot-test"     "fbca04" "Trigger Copilot Test Generator"
create_label "copilot-docs"     "006b75" "Trigger Copilot Doc Generator"
create_label "copilot-fix"      "d93f0b" "Trigger Copilot CI Fix Agent"
create_label "in-progress"      "ededed" "Agent is working on this"
create_label "ready-for-review" "0075ca" "Agent completed, ready for review"
create_label "agent-error"      "b60205" "Agent encountered an error"

success "Labels created"

# ---------------------------------------------------------------------------
# Step 3: Detect default branch and get its SHA
# ---------------------------------------------------------------------------
info "Step 3/6: Detecting default branch ..."

DEFAULT_BRANCH=$(gh api --hostname "${GHES_HOST}" \
    "/repos/${OWNER}/${REPO}" --jq '.default_branch' 2>/dev/null) \
    || DEFAULT_BRANCH="main"

BASE_SHA=$(gh api --hostname "${GHES_HOST}" \
    "/repos/${OWNER}/${REPO}/git/ref/heads/${DEFAULT_BRANCH}" \
    --jq '.object.sha' 2>/dev/null) \
    || fatal "Could not get SHA for branch '${DEFAULT_BRANCH}'. Does the repo exist?"

success "Default branch: ${DEFAULT_BRANCH} (${BASE_SHA:0:7})"

# ---------------------------------------------------------------------------
# Step 3: Create branch
# ---------------------------------------------------------------------------
info "Step 4/6: Creating branch '${BRANCH_NAME}' ..."

gh api --hostname "${GHES_HOST}" \
    "/repos/${OWNER}/${REPO}/git/refs" \
    -f "ref=refs/heads/${BRANCH_NAME}" \
    -f "sha=${BASE_SHA}" \
    --silent 2>/dev/null \
    || warn "Branch may already exist, continuing..."

success "Branch created"

# ---------------------------------------------------------------------------
# Step 4: Create workflow files
# ---------------------------------------------------------------------------
info "Step 5/6: Creating workflow files ..."

# Helper: create or update file via API
create_file() {
    local path="$1"
    local content="$2"
    local message="$3"

    local encoded
    encoded=$(echo -n "$content" | base64)

    local existing_sha
    existing_sha=$(gh api --hostname "${GHES_HOST}" \
        "/repos/${OWNER}/${REPO}/contents/${path}?ref=${BRANCH_NAME}" \
        --jq '.sha' 2>/dev/null) || existing_sha=""

    local api_args=(
        --hostname "${GHES_HOST}"
        "/repos/${OWNER}/${REPO}/contents/${path}"
        -f "message=${message}"
        -f "content=${encoded}"
        -f "branch=${BRANCH_NAME}"
    )

    if [ -n "$existing_sha" ]; then
        api_args+=(-f "sha=${existing_sha}")
    fi

    local api_error
    if ! api_error=$(gh api -X PUT "${api_args[@]}" --silent 2>&1); then
        if [[ "$path" == .github/workflows/* ]]; then
            fatal "Failed to create ${path}. The deployment token must include the 'workflow' scope to create or update GitHub Actions workflow files. gh api error: ${api_error}"
        fi
        fatal "Failed to create ${path}. gh api error: ${api_error}"
    fi

    success "  Created ${path}"
}

if [ "$STANDALONE" = true ]; then
    # -----------------------------------------------------------------------
    # STANDALONE MODE: Deploy master workflows directly from central repo
    # This avoids cross-repo reusable workflow access issues on GHES
    # -----------------------------------------------------------------------
    info "Standalone mode: fetching master workflows from ${OWNER}/${CENTRAL_REPO} ..."

    WORKFLOW_FILES=(
        "copilot-coder-master.yml"
        "copilot-reviewer-master.yml"
        "copilot-docs-master.yml"
        "ci-fix-master.yml"
    )

    for wf in "${WORKFLOW_FILES[@]}"; do
        info "  Downloading ${wf} ..."
        CONTENT=$(gh api --hostname "${GHES_HOST}" \
            "/repos/${OWNER}/${CENTRAL_REPO}/contents/.github/workflows/${wf}" \
            --jq '.content' 2>/dev/null | base64 -d) \
            || fatal "Failed to download ${wf} from ${OWNER}/${CENTRAL_REPO}"

        # Deploy with simplified name (remove -master suffix)
        TARGET_NAME="${wf//-master/}"
        create_file ".github/workflows/${TARGET_NAME}" \
            "$CONTENT" \
            "ci: add ${TARGET_NAME} (standalone)"
    done

    # Also deploy the agent Python code
    info "  Deploying agent Python module ..."

    copy_agent_tree() {
        local dir_path="$1"
        local entries
        entries=$(gh api --hostname "${GHES_HOST}" \
            "/repos/${OWNER}/${CENTRAL_REPO}/contents/${dir_path}" \
            --jq '.[] | [.type, .path] | @tsv' 2>/dev/null) \
            || fatal "Could not list ${dir_path} from ${OWNER}/${CENTRAL_REPO}"

        while IFS=$'\t' read -r entry_type filepath; do
            if [ -z "$filepath" ]; then
                continue
            fi
            if [ "$entry_type" = "dir" ]; then
                copy_agent_tree "$filepath"
                continue
            fi

            FILE_CONTENT=$(gh api --hostname "${GHES_HOST}" \
                "/repos/${OWNER}/${CENTRAL_REPO}/contents/${filepath}" \
                --jq '.content' 2>/dev/null | base64 -d 2>/dev/null) \
                || fatal "Failed to download ${filepath} from ${OWNER}/${CENTRAL_REPO}"
            create_file "${filepath}" "$FILE_CONTENT" "ci: add ${filepath}"
        done <<< "$entries"
    }

    copy_agent_tree "agent"

    success "Standalone workflows deployed"
else
    # --- copilot-coder.yml ---
CODER_WORKFLOW=$(cat <<'YAML'
name: "Copilot Coder Agent"
on:
  issues:
    types: [labeled]

jobs:
  copilot-coder:
    if: |
      github.event_name == 'issues' && github.event.label.name == 'copilot'
    uses: ${CENTRAL_REPO_FULL}/.github/workflows/copilot-coder-master.yml@main
    with:
      agent_repo: ${CENTRAL_REPO_FULL}
    secrets:
      GH_TOKEN: ${{ secrets.GH_TOKEN }}
      COPILOT_GITHUB_TOKEN: ${{ secrets.COPILOT_GITHUB_TOKEN }}
YAML
)
CODER_WORKFLOW="${CODER_WORKFLOW//\$\{CENTRAL_REPO_FULL\}/${OWNER}\/${CENTRAL_REPO}}"

# --- copilot-reviewer.yml ---
REVIEWER_WORKFLOW=$(cat <<'YAML'
name: "Copilot Code Reviewer"
on:
  pull_request:
    types: [labeled]

jobs:
  copilot-reviewer:
    if: |
      github.event_name == 'pull_request' && github.event.label.name == 'copilot-review'
    uses: ${CENTRAL_REPO_FULL}/.github/workflows/copilot-reviewer-master.yml@main
    with:
      agent_repo: ${CENTRAL_REPO_FULL}
    secrets:
      GH_TOKEN: ${{ secrets.GH_TOKEN }}
      COPILOT_GITHUB_TOKEN: ${{ secrets.COPILOT_GITHUB_TOKEN }}
YAML
)
REVIEWER_WORKFLOW="${REVIEWER_WORKFLOW//\$\{CENTRAL_REPO_FULL\}/${OWNER}\/${CENTRAL_REPO}}"

create_file ".github/workflows/copilot-coder.yml" \
    "$CODER_WORKFLOW" \
    "ci: add Copilot coder agent workflow"

# --- copilot-docs.yml ---
DOCS_WORKFLOW=$(cat <<'YAML'
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
    uses: ${CENTRAL_REPO_FULL}/.github/workflows/copilot-docs-master.yml@main
    with:
      agent_repo: ${CENTRAL_REPO_FULL}
    secrets:
      GH_TOKEN: ${{ secrets.GH_TOKEN }}
      COPILOT_GITHUB_TOKEN: ${{ secrets.COPILOT_GITHUB_TOKEN }}
YAML
)
DOCS_WORKFLOW="${DOCS_WORKFLOW//\$\{CENTRAL_REPO_FULL\}/${OWNER}\/${CENTRAL_REPO}}"

create_file ".github/workflows/copilot-reviewer.yml" \
    "$REVIEWER_WORKFLOW" \
    "ci: add Copilot reviewer agent workflow"

create_file ".github/workflows/copilot-docs.yml" \
    "$DOCS_WORKFLOW" \
    "ci: add Copilot docs agent workflow"

success "All caller workflow files created"

fi  # end of standalone/caller mode

success "Workflow deployment complete"

# ---------------------------------------------------------------------------
# Step 5b: Write output language into .github/ghes-agent.yml
# ---------------------------------------------------------------------------
info "Writing output language preference (lang=${LANG_CHOICE}) to .github/ghes-agent.yml ..."

AGENT_YAML_PATH=".github/ghes-agent.yml"
EXISTING_YAML=""
EXISTING_YAML_SHA=""

if EXISTING_JSON=$(gh api --hostname "${GHES_HOST}" \
        "/repos/${OWNER}/${REPO}/contents/${AGENT_YAML_PATH}?ref=${BRANCH_NAME}" 2>/dev/null); then
    EXISTING_YAML_SHA=$(echo "$EXISTING_JSON" | jq -r '.sha // empty')
    EXISTING_YAML=$(echo "$EXISTING_JSON" | jq -r '.content' | base64 -d 2>/dev/null || echo "")
fi

if [ -n "$EXISTING_YAML" ]; then
    # Preserve user's existing keys; replace or append output_language only.
    NEW_YAML=$(printf '%s\n' "$EXISTING_YAML" | grep -v -E '^[[:space:]]*output_language[[:space:]]*:' || true)
    # Ensure trailing newline, then append the key.
    NEW_YAML="${NEW_YAML%$'\n'}"$'\n'"output_language: ${LANG_CHOICE}"$'\n'
    COMMIT_MSG="chore: set output_language=${LANG_CHOICE} in ghes-agent.yml"
else
    NEW_YAML=$(cat <<YAML
# ghes-coding-agent per-repository configuration
# Full reference: https://github.com/your-org/ghes-coding-agent/blob/main/docs/SETUP.md
output_language: ${LANG_CHOICE}
YAML
)
    COMMIT_MSG="chore: add ghes-agent.yml (output_language=${LANG_CHOICE})"
fi

ENCODED_YAML=$(printf '%s' "$NEW_YAML" | base64)

API_ARGS=(
    --hostname "${GHES_HOST}"
    "/repos/${OWNER}/${REPO}/contents/${AGENT_YAML_PATH}"
    -f "message=${COMMIT_MSG}"
    -f "content=${ENCODED_YAML}"
    -f "branch=${BRANCH_NAME}"
)
if [ -n "$EXISTING_YAML_SHA" ]; then
    API_ARGS+=(-f "sha=${EXISTING_YAML_SHA}")
fi

gh api -X PUT "${API_ARGS[@]}" --silent 2>/dev/null \
    || fatal "Failed to write ${AGENT_YAML_PATH}"

success "  ${AGENT_YAML_PATH} updated (output_language=${LANG_CHOICE})"

# ---------------------------------------------------------------------------
# Step 5: Create pull request
# ---------------------------------------------------------------------------
info "Step 6/6: Creating pull request ..."

PR_BODY=$(cat <<EOF
## 🤖 Copilot Agent Workflows

This PR adds GitHub Actions workflows that integrate with the [GHES Coding Agent](${GHES_URL}/${OWNER}/${CENTRAL_REPO}).

### Workflows Added

| Workflow | Trigger | Description |
|----------|---------|-------------|
| \`copilot-coder.yml\` | \`copilot\` label | Autonomous code generation from issues |
| \`copilot-reviewer.yml\` | \`copilot-review\` label | Multi-model AI code review |
| \`copilot-docs.yml\` | \`copilot-docs\` label | Auto documentation generation |

### Agent Output Language

This repository is configured to receive agent-authored comments, PR bodies, and code-review messages in **\`${LANG_CHOICE}\`** (see \`.github/ghes-agent.yml\`). To change it later, edit the \`output_language\` key in that file.

### Setup Required

After merging, add these repository secrets:

| Secret | Required | Description |
|--------|----------|-------------|
| \`GH_TOKEN\` | ✅ | GHES PAT with \`repo\` scope — for GHES API and git operations. Add \`workflow\` only if the agent should be allowed to modify \`.github/workflows/*\` later. |
| \`COPILOT_GITHUB_TOKEN\` | ❌ | GitHub token for Copilot SDK auth. If not set, runner \`copilot login\` credentials are used |

### How to Use

1. Create an issue describing a feature or bug fix
2. Add the \`copilot\` label, or create a PR with \`copilot-review\` label
3. The agent will analyze, implement, and open a PR

---
*Deployed by GHES Coding Agent setup script*
EOF
)

PR_URL=$(gh api --hostname "${GHES_HOST}" \
    "/repos/${OWNER}/${REPO}/pulls" \
    -f "title=ci: Add Copilot Agent workflows (output: ${LANG_CHOICE})" \
    -f "body=${PR_BODY}" \
    -f "head=${BRANCH_NAME}" \
    -f "base=${DEFAULT_BRANCH}" \
    --jq '.html_url' 2>/dev/null) \
    || fatal "Failed to create pull request"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "========================================="
echo "🎉 Deployment complete!"
echo "========================================="
echo ""
echo "  Pull Request: ${PR_URL}"
echo ""
echo "  Next steps:"
echo "    1. Review and merge the PR above"
echo "    2. Add repository secrets:"
echo "       - GH_TOKEN (required): GHES PAT with 'repo' scope"
echo "         Add 'workflow' only if the agent should modify .github/workflows/* later"
echo "       - COPILOT_GITHUB_TOKEN (optional): GitHub token for Copilot SDK auth"
echo "         If not set, SDK/CLI uses runner 'copilot login' credentials"
echo "    3. Create an issue and add the 'copilot' label to test"
echo ""
echo "  For more details, see: docs/SETUP.md"
echo ""
