# Copilot SDK sample — Coding-agent-ish implementation on GHES Setup Guide

이 가이드는 GitHub Enterprise Server 환경에 Copilot SDK 기반의 Coding agent 유사 구현체를 설치하고 구성하는 전체 과정을 설명합니다.

---

## Prerequisites (사전 요구사항)

| 항목 | 최소 요구사항 | 비고 |
|------|-------------|------|
| **GHES 버전** | 3.16+ | Reusable workflows 지원 필수 |
| **Self-hosted runner** | Ubuntu 20.04+ 또는 RHEL 8+ | `actions-runner` 설치됨 |
| **Python** | 3.11+ | async/await, type hints |
| **Node.js** | 22.x+ | Copilot CLI 실행에 필요 |
| **GitHub CLI** | 2.x+ | `gh` 명령어 |
| **네트워크** | Copilot API 접근 가능 | runner에서 `api.github.com` 접근 가능 |
| **PAT** | GHES PAT + Copilot 인증 | GHES API 토큰과 Copilot 인증 토큰은 별도 |

> **Air-gapped 환경**: Copilot SDK는 클라우드 AI 모델과 통신합니다. 완전한 폐쇄망에서는 동작하지 않습니다.

---

## Step 1: 중앙 리포지토리 클론/포크

GHES 인스턴스에 이 리포지토리를 클론하거나 포크합니다.

```bash
# Option A: gh CLI를 사용한 포크
gh repo fork ghes-coding-agent --org YOUR_ORG --hostname ghes.example.com

# Option B: 직접 클론 후 push
git clone https://github.com/your-source/ghes-coding-agent.git
cd ghes-coding-agent
git remote set-url origin https://ghes.example.com/YOUR_ORG/ghes-coding-agent.git
git push -u origin main
```

---

## Step 2: Secrets 구성

중앙 리포지토리에 다음 시크릿을 설정합니다.

### Repository Secrets

Settings → Secrets and variables → Actions → New repository secret

| Secret 이름 | Required | 설명 | 예시 |
|-------------|----------|------|------|
| `GH_TOKEN` | O | GHES PAT (`repo`, `read:org` scope; 필요 시 `workflow`) — GHES API 및 git 인증 | `github_pat_xxxx` 또는 `ghp_xxxx` |
| `COPILOT_GITHUB_TOKEN` | X | Copilot SDK 인증용 GitHub 토큰. 미설정 시 러너의 `copilot login` 사용 | `github_pat_xxxx` (GitHub.com) |

### 인증 시나리오

GHES API와 Copilot SDK는 **별도의 자격증명**을 사용합니다. 환경에 따라 아래 2가지 시나리오 중 하나를 선택하세요:

#### 시나리오 1: Separate Copilot auth

1. `GH_TOKEN`: GHES 인스턴스의 PAT (GHES API용)
2. `COPILOT_GITHUB_TOKEN`: GitHub.com의 fine-grained PAT (Copilot 접근 권한 필요)
3. 시크릿: `GH_TOKEN` + `COPILOT_GITHUB_TOKEN` 모두 추가

#### 시나리오 2: Pre-authenticated runner

Self-hosted runner에서 사전 인증:

1. Runner VM에서 `copilot login` 실행 (일회성, 대화형 OAuth)
2. `GH_TOKEN`만 시크릿으로 추가
3. Copilot SDK/CLI가 저장된 OAuth 자격증명을 사용

> `COPILOT_GITHUB_TOKEN`은 Copilot SDK의 **공식** 환경변수입니다 (최우선 순위).
> 토큰 사용자에게 **Copilot 라이선스**가 할당되어 있어야 합니다.

### PAT 생성 방법

1. GHES → Settings → Developer settings → Personal access tokens
2. Classic PAT (`ghp_`) 또는 GHES에서 지원하는 Fine-grained PAT (`github_pat_`) 생성
3. 필요한 scope (Classic PAT의 경우):
   - `repo` (Full control of private repositories)
   - `read:org` (Read org membership)
    - `workflow` (조건부: 설치 스크립트처럼 `.github/workflows/*` 파일을 생성/수정할 때 필요. 일반적인 agent 런타임에는 불필요)
4. 런타임 토큰은 대상 리포지토리/중앙 리포지토리 시크릿에 `GH_TOKEN`으로 저장

> `GH_TOKEN`은 Copilot 인증에 사용하지 않습니다. Copilot 인증은 `COPILOT_GITHUB_TOKEN` 또는 runner의 `copilot login`을 사용합니다.

```bash
# 토큰이 올바른지 확인
export GH_TOKEN="ghp_xxxxxxxxxxxx"
gh auth login --hostname ghes.example.com --with-token <<< "$GH_TOKEN"
gh api --hostname ghes.example.com /user --jq '.login'
```

---

## Step 3: Reusable Workflows 접근 허용

조직 내 다른 리포지토리에서 중앙 리포지토리의 workflows를 호출하려면 접근을 허용해야 합니다.

### 조직 레벨 설정

1. **Organization Settings** → **Actions** → **General**
2. **Actions permissions** → "Allow all actions and reusable workflows" 선택
3. 또는 특정 리포지토리만 허용: "Allow select actions and reusable workflows" → `YOUR_ORG/ghes-coding-agent` 추가

### 리포지토리 레벨 설정

중앙 리포지토리:

1. **Settings** → **Actions** → **General**
2. **Access** 섹션에서 "Accessible from repositories in the organization" 선택

---

## Step 4: Self-Hosted Runner 설정

### 자동 설정 (권장)

```bash
# 스크립트를 runner VM에 복사 후 실행
sudo ./scripts/setup-runner.sh
```

이 스크립트가 자동으로 설치하는 항목:
- Python 3.11+
- Node.js 22.x
- GitHub CLI (`gh`)
- Copilot CLI
- `uv` (Python 패키지 매니저)
- 프로젝트 의존성

### 수동 설정

<details>
<summary>수동 설치 단계 (클릭하여 펼치기)</summary>

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

# 6. 의존성
pip install -r requirements.txt
```
</details>

### Runner 등록

```bash
# Runner 바이너리 다운로드 (GHES 관리자 페이지에서 URL 확인)
./config.sh \
    --url https://ghes.example.com/YOUR_ORG/ghes-coding-agent \
    --token RUNNER_REGISTRATION_TOKEN \
    --labels copilot-agent \
    --name "copilot-runner-01"

# 서비스로 실행
sudo ./svc.sh install
sudo ./svc.sh start
```

> Runner에 `copilot-agent` 레이블을 추가하면 workflows에서 `runs-on: [self-hosted, copilot-agent]`로 대상 runner를 지정할 수 있습니다.

---

## Step 4-1: 레포별 설정 파일

각 대상 리포지토리에서 `.github/ghes-agent.yml` 파일로 에이전트 동작을 커스터마이징할 수 있습니다.

```yaml
# .github/ghes-agent.yml (대상 리포지토리에 생성)
default_branch: develop          # PR 타겟 브랜치 (기본: main)
timeout_minutes: 45              # 에이전트 타임아웃 (기본: 30)
max_retries: 5                   # 재시도 횟수 (기본: 3)
output_language: ko              # 에이전트 출력 언어: en | ko (기본: en)
branch_prefix: copilot/          # 에이전트 브랜치 접두사
coder_model: claude-sonnet-4.6   # 코드 생성 모델
coder_pr_summary_model: gpt-5.4-mini   # PR 본문 요약용 경량 모델
reviewer_models:                 # 리뷰 모델 목록
  - claude-opus-4.6
  - gpt-5.4
reviewer_summary_model: claude-opus-4.6      # 통합 요약 모델
reviewer_suggestion_model: claude-opus-4.6   # inline suggestion 변환 모델
```

| 필드 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| `default_branch` | `string` | `main` | PR 생성 시 base 브랜치 |
| `timeout_minutes` | `int` | `30` | 에이전트 최대 실행 시간 (분) |
| `max_retries` | `int` | `3` | Copilot API 재시도 횟수 |
| `output_language` | `string` | `en` | 에이전트가 생성하는 리뷰 코멘트, PR 본문, 요약, inline suggestion `EXPLANATION` 텍스트의 언어. `en` 또는 `ko`. 코드·식별자·커밋 메시지·파일 경로·파서가 요구하는 마커는 원문 그대로 유지됩니다. |
| `branch_prefix` | `string` | `copilot/` | 에이전트가 생성하는 브랜치 접두사 |
| `coder_model` | `string` | `claude-sonnet-4.6` | Coder/Doc/CI Fix 에이전트 모델 |
| `coder_pr_summary_model` | `string` | `gpt-5.4-mini` | CoderAgent가 생성한 변경의 PR 본문 요약을 작성하는 경량 모델 |
| `reviewer_models` | `list[string]` | `claude-opus-4.6`, `gpt-5.4` | ReviewerAgent가 병렬 실행하는 모델 목록 |
| `reviewer_summary_model` | `string` | 첫 번째 리뷰 모델 | 다중 모델 리뷰 결과를 통합하는 모델 |
| `reviewer_suggestion_model` | `string` | 통합 요약 모델 | 채택된 finding을 inline Suggested Changes 형식으로 변환하는 모델 |

> 파일이 없으면 글로벌 기본값이 적용됩니다. 필요한 필드만 오버라이드하면 됩니다.
> `output_language`는 `./scripts/deploy-to-repo.sh ... --lang ko` 로도 자동 주입할 수 있습니다 (아래 Step 5 참고).

---

## Step 5: 대상 리포지토리에 배포

### 자동 배포 (권장)

```bash
# Bash (Linux/macOS) — 기본 (caller 워크플로우, 영어 출력)
./scripts/deploy-to-repo.sh ghes.example.com YOUR_ORG target-repo "$GH_TOKEN" ghes-coding-agent

# Bash — 한국어 출력 + standalone 모드 (reusable workflow 접근 제약이 있을 때)
./scripts/deploy-to-repo.sh ghes.example.com YOUR_ORG target-repo "$GH_TOKEN" ghes-coding-agent \
    --standalone --lang ko

# PowerShell (Windows)
.\scripts\deploy-to-repo.ps1 -GhesHost ghes.example.com -Owner YOUR_ORG -Repo target-repo -Token $env:GH_TOKEN -Lang ko
```

> 배포 스크립트에 넘기는 토큰은 대상 리포지토리에 `.github/workflows/*` 파일을 생성하므로 `workflow` scope가 필요합니다. 배포 후 caller workflow가 런타임에 사용하는 대상 리포지토리의 `GH_TOKEN`은 일반적인 이슈/PR/git 작업에는 `repo` scope면 충분하며, agent가 나중에 workflow 파일 자체를 수정해야 하는 경우에만 `workflow`를 추가하세요.

주요 옵션:

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--lang en\|ko` / `-Lang` | `en` | 에이전트가 생성하는 리뷰·PR 본문·요약·inline suggestion `EXPLANATION` 텍스트의 언어. `.github/ghes-agent.yml`의 `output_language`에 기록됩니다. |
| `--standalone` (bash 전용) | off | 조직 단위 reusable workflow 접근이 허용되지 않는 환경에서 전체 standalone 워크플로우를 배포합니다. 대부분의 GHES 환경에서 권장. |

이 스크립트는:
1. 대상 리포지토리에 새 브랜치 생성
2. Caller(또는 standalone) workflow 파일 생성
3. `.github/ghes-agent.yml`의 `output_language` 키를 지정된 값으로 설정
4. PR 생성

### 수동 배포

대상 리포지토리의 `.github/workflows/` 디렉토리에 caller workflow 파일을 직접 추가할 수 있습니다. 예시:

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

### 대상 리포지토리 시크릿 설정

배포 후, 대상 리포지토리에도 시크릿을 추가합니다:

1. **Settings** → **Secrets and variables** → **Actions**
2. `GH_TOKEN` 추가 (GHES API 및 git 인증용; 일반 런타임은 `repo` scope면 충분)
3. (선택) `COPILOT_GITHUB_TOKEN` 추가 (runner에서 `copilot login`을 사용하지 않는 경우)

---

## Step 6: 테스트

### 첫 번째 테스트: Coder Agent

1. 대상 리포지토리에서 새 이슈 생성:
   - **Title**: "Add hello world endpoint"
   - **Body**: "Create a simple HTTP endpoint that returns 'Hello, World!'"
2. 이슈에 `copilot` 레이블 추가
3. Actions 탭에서 워크플로우 실행 확인
4. 에이전트가 PR을 생성하면 성공! 

### 두 번째 테스트: Code Review

1. PR을 열고 `copilot-review` 레이블 추가
2. 두 AI 모델의 리뷰 코멘트 확인

---

## Troubleshooting (문제 해결)

### Quick Checks

```bash
# Runner 상태 확인
sudo ./svc.sh status

# GHES 연결 확인
gh api --hostname ghes.example.com /meta --jq '.installed_version'

# Copilot CLI 확인
copilot --version

# Python 의존성 확인
python3 -c "import agent; print('OK')"
```

### 로그 확인

```bash
# Runner 로그
journalctl -u actions.runner.* -f

# Workflow 로그
gh run list --hostname ghes.example.com --repo YOUR_ORG/target-repo --limit 5
gh run view <run-id> --hostname ghes.example.com --repo YOUR_ORG/target-repo --log
```

---

## 관련 문서

- [ARCHITECTURE.md](./ARCHITECTURE.md) — 시스템 아키텍처
- [README.md](../README.md) — 프로젝트 개요
