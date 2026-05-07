#!/bin/bash
# Setup script for self-hosted runner prerequisites
# Run this on your GHES self-hosted runner VM
#
# Supported OS: Ubuntu 20.04+, RHEL/CentOS 8+
# Usage: sudo ./setup-runner.sh [--skip-verify]

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PYTHON_MIN_VERSION="3.11"
NODE_VERSION="22"
SKIP_VERIFY=false

for arg in "$@"; do
    case "$arg" in
        --skip-verify) SKIP_VERIFY=true ;;
        --help|-h)
            echo "Usage: sudo $0 [--skip-verify]"
            echo ""
            echo "Options:"
            echo "  --skip-verify   Skip final verification step"
            echo "  --help, -h      Show this help message"
            exit 0
            ;;
        *)
            echo "❌ Unknown argument: $arg"
            exit 1
            ;;
    esac
done

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

command_exists() { command -v "$1" &>/dev/null; }

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID="${ID}"
        OS_VERSION="${VERSION_ID:-}"
    else
        fatal "Cannot detect OS. /etc/os-release not found."
    fi
}

ensure_root() {
    if [ "$(id -u)" -ne 0 ]; then
        fatal "This script must be run as root (use sudo)."
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo ""
echo "🚀 GHES Coding Agent - Runner Setup"
echo "====================================="
echo ""

ensure_root
detect_os

info "Detected OS: ${OS_ID} ${OS_VERSION}"

# ===========================================================================
# 1. Install Python 3.11+
# ===========================================================================
echo ""
info "Step 1/7: Installing Python ${PYTHON_MIN_VERSION}+ ..."

install_python_ubuntu() {
    apt-get update -qq
    apt-get install -y software-properties-common

    # Detect current Python version and install venv for it
    if command_exists python3; then
        CURRENT_PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        if [ -n "$CURRENT_PY_VER" ]; then
            info "Installing venv for existing Python ${CURRENT_PY_VER}..."
            apt-get install -y "python${CURRENT_PY_VER}-venv" "python${CURRENT_PY_VER}-dev" 2>/dev/null || true
        fi
    fi

    # If Python 3.11+ not available, install from deadsnakes
    add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
    apt-get update -qq
    # Install python3.11 + venv, but also ensure current python's venv is installed
    apt-get install -y python3.11 python3.11-venv python3.11-dev 2>/dev/null || true
    # Also install venv for python3.12 if that's the system default
    apt-get install -y python3.12-venv 2>/dev/null || true
    apt-get install -y python3-venv 2>/dev/null || true
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 2>/dev/null || true
}

install_python_rhel() {
    dnf install -y python3.11 python3.11-devel python3.11-pip 2>/dev/null \
        || yum install -y python3.11 python3.11-devel python3.11-pip 2>/dev/null \
        || {
            warn "Python 3.11 not in default repos, trying EPEL + CRB..."
            dnf install -y epel-release 2>/dev/null || yum install -y epel-release 2>/dev/null
            dnf config-manager --set-enabled crb 2>/dev/null || true
            dnf install -y python3.11 python3.11-devel python3.11-pip 2>/dev/null \
                || fatal "Could not install Python 3.11. Please install manually."
        }
}

if command_exists python3.11; then
    success "Python 3.11 already installed"
elif command_exists python3; then
    CURRENT_PY=$(python3 --version 2>&1 | awk '{print $2}')
    MAJOR=$(echo "$CURRENT_PY" | cut -d. -f1)
    MINOR=$(echo "$CURRENT_PY" | cut -d. -f2)
    if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 11 ]; then
        success "Python ${CURRENT_PY} meets minimum requirement"
    else
        warn "Python ${CURRENT_PY} found but ${PYTHON_MIN_VERSION}+ required"
        case "$OS_ID" in
            ubuntu|debian) install_python_ubuntu ;;
            rhel|centos|rocky|almalinux|fedora) install_python_rhel ;;
            *) fatal "Unsupported OS: $OS_ID. Install Python ${PYTHON_MIN_VERSION}+ manually." ;;
        esac
    fi
else
    case "$OS_ID" in
        ubuntu|debian) install_python_ubuntu ;;
        rhel|centos|rocky|almalinux|fedora) install_python_rhel ;;
        *) fatal "Unsupported OS: $OS_ID. Install Python ${PYTHON_MIN_VERSION}+ manually." ;;
    esac
fi

success "Python installed"

# ===========================================================================
# 2. Install Node.js 22.x
# ===========================================================================
echo ""
info "Step 2/7: Installing Node.js ${NODE_VERSION}.x ..."

install_node_ubuntu() {
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" | bash -
    apt-get install -y nodejs
}

install_node_rhel() {
    curl -fsSL "https://rpm.nodesource.com/setup_${NODE_VERSION}.x" | bash -
    dnf install -y nodejs 2>/dev/null || yum install -y nodejs
}

if command_exists node; then
    NODE_VER=$(node --version | sed 's/^v//' | cut -d. -f1)
    if [ "$NODE_VER" -ge "$NODE_VERSION" ]; then
        success "Node.js v$(node --version | sed 's/^v//') meets requirement"
    else
        warn "Node.js v$(node --version | sed 's/^v//') found, need ${NODE_VERSION}+"
        case "$OS_ID" in
            ubuntu|debian) install_node_ubuntu ;;
            rhel|centos|rocky|almalinux|fedora) install_node_rhel ;;
            *) fatal "Unsupported OS for Node.js auto-install." ;;
        esac
    fi
else
    case "$OS_ID" in
        ubuntu|debian) install_node_ubuntu ;;
        rhel|centos|rocky|almalinux|fedora) install_node_rhel ;;
        *) fatal "Unsupported OS for Node.js auto-install." ;;
    esac
fi

success "Node.js installed"

# ===========================================================================
# 3. Install GitHub CLI (gh)
# ===========================================================================
echo ""
info "Step 3/7: Installing GitHub CLI ..."

install_gh_ubuntu() {
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    apt-get update -qq
    apt-get install -y gh
}

install_gh_rhel() {
    dnf install -y 'dnf-command(config-manager)' 2>/dev/null || true
    dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo 2>/dev/null \
        || yum-config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo 2>/dev/null
    dnf install -y gh 2>/dev/null || yum install -y gh
}

if command_exists gh; then
    success "GitHub CLI already installed: $(gh --version | head -1)"
else
    case "$OS_ID" in
        ubuntu|debian) install_gh_ubuntu ;;
        rhel|centos|rocky|almalinux|fedora) install_gh_rhel ;;
        *) fatal "Unsupported OS for gh auto-install." ;;
    esac
    success "GitHub CLI installed"
fi

# ===========================================================================
# 4. Install Copilot CLI
# ===========================================================================
echo ""
info "Step 4/7: Installing Copilot CLI ..."

npm install -g @githubnext/github-copilot-cli@latest 2>/dev/null \
    || npm install -g @github/copilot@latest 2>/dev/null \
    || warn "Copilot CLI package not yet publicly available — install manually when released"

if command_exists copilot; then
    success "Copilot CLI installed: $(copilot --version 2>/dev/null || echo 'version unknown')"
    info "  Copilot CLI auth options:"
    info "    1. Set COPILOT_GITHUB_TOKEN env var"
    info "    2. Run 'copilot login' on this runner (interactive OAuth, one-time)"
else
    warn "Copilot CLI binary not found in PATH. You may need to install it manually."
fi

# ===========================================================================
# 5. Install uv (Python package manager)
# ===========================================================================
echo ""
info "Step 5/7: Installing uv (fast Python package manager) ..."

if command_exists uv; then
    success "uv already installed: $(uv --version)"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if command_exists uv; then
        success "uv installed: $(uv --version)"
    else
        warn "uv installed but not in PATH. Add ~/.local/bin to PATH."
    fi
fi

# ===========================================================================
# 6. Install pip dependencies
# ===========================================================================
echo ""
info "Step 6/7: Installing Python dependencies ..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
REQ_FILE="${REPO_ROOT}/requirements.txt"
VENV_DIR="/opt/ghes-agent-venv"

# PEP 668 (externally-managed-environment) 대응:
# Ubuntu 23.04+, Debian 12+ 등에서 시스템 Python에 직접 pip install을 차단함.
# 항상 venv를 사용하여 의존성을 설치합니다.

setup_venv() {
    info "Creating virtual environment at ${VENV_DIR} ..."

    # python3-venv 패키지가 필요할 수 있음 (Ubuntu/Debian의 경우 pythonX.Y-venv 필요)
    if ! python3 -m venv --help &>/dev/null; then
        warn "python3-venv not available. Installing..."
        case "$OS_ID" in
            ubuntu|debian)
                apt-get update -qq
                # Install venv for the exact Python version in use
                PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
                apt-get install -y "python${PY_VER}-venv" 2>/dev/null \
                    || apt-get install -y python3-venv python3-full 2>/dev/null \
                    || apt-get install -y python3-venv
                ;;
            rhel|centos|rocky|almalinux|fedora)
                dnf install -y python3-virtualenv 2>/dev/null \
                    || yum install -y python3-virtualenv 2>/dev/null \
                    || true
                ;;
        esac
    fi

    python3 -m venv "$VENV_DIR"
    success "Virtual environment created"
}

install_deps_in_venv() {
    # Activate venv
    source "${VENV_DIR}/bin/activate"

    # Upgrade pip inside venv
    pip install --upgrade pip --quiet

    if [ -f "$REQ_FILE" ]; then
        pip install -r "$REQ_FILE" --quiet
        success "Python dependencies installed in venv"
    else
        warn "requirements.txt not found at ${REQ_FILE}. Skipping."
    fi

    deactivate
}

# Create venv and install
setup_venv
install_deps_in_venv

# Create a wrapper script so 'ghes-agent' uses the venv Python
WRAPPER="/usr/local/bin/ghes-agent"
cat > "$WRAPPER" << EOF
#!/bin/bash
# Auto-generated wrapper to run GHES Coding Agent with venv Python
exec "${VENV_DIR}/bin/python" -m agent.orchestrator "\$@"
EOF
chmod +x "$WRAPPER"
success "Created wrapper: ${WRAPPER}"

# Also create a symlink for workflows to use
VENV_PYTHON="${VENV_DIR}/bin/python"
info "Workflows should use: ${VENV_PYTHON} -m agent.orchestrator"
info "Or simply: ghes-agent"

# ===========================================================================
# 7. Verify installations
# ===========================================================================
echo ""
info "Step 7/7: Verifying installations ..."

if [ "$SKIP_VERIFY" = true ]; then
    warn "Verification skipped (--skip-verify)"
else
    ERRORS=0

    check_cmd() {
        local cmd="$1"
        local label="$2"
        local version_flag="${3:---version}"
        if command_exists "$cmd"; then
            VER=$($cmd $version_flag 2>&1 | head -1)
            success "${label}: ${VER}"
        else
            error "${label}: NOT FOUND"
            ERRORS=$((ERRORS + 1))
        fi
    }

    echo ""
    echo "┌────────────────────────────────────────┐"
    echo "│       Installation Verification        │"
    echo "├────────────────────────────────────────┤"

    check_cmd python3   "Python"      "--version"
    check_cmd pip3     "pip"         "--version"
    check_cmd node      "Node.js"     "--version"
    check_cmd npm       "npm"         "--version"
    check_cmd gh        "GitHub CLI"  "--version"
    check_cmd git       "Git"         "--version"

    if command_exists uv; then
        check_cmd uv "uv" "--version"
    fi

    if command_exists copilot; then
        check_cmd copilot "Copilot CLI" "--version"
    else
        warn "Copilot CLI: not installed (install when available)"
    fi

    echo "└────────────────────────────────────────┘"
    echo ""

    if [ "$ERRORS" -gt 0 ]; then
        fatal "${ERRORS} required tool(s) missing. Please fix and re-run."
    fi
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "====================================="
echo "🎉 Runner setup complete!"
echo "====================================="
echo ""
echo "Next steps:"
echo "  1. Configure the runner as a GitHub Actions self-hosted runner"
echo "  2. Set repository secrets:"
echo "     - GH_TOKEN (required): GHES PAT for API and git operations"
echo "     - COPILOT_GITHUB_TOKEN (optional): GitHub token for Copilot SDK auth"
echo "       If not set, SDK/CLI uses runner 'copilot login' credentials"
echo "  3. (Optional) Run 'copilot login' on this runner for persistent Copilot auth"
echo "  4. Deploy workflows with: ./scripts/deploy-to-repo.sh"
echo ""
echo "For full documentation, see: docs/SETUP.md"
echo ""
