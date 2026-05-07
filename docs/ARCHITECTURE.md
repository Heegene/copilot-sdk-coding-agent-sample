# GHES Coding Agent — Architecture

이 문서는 GHES Coding Agent의 시스템 아키텍처, 컴포넌트 구조, 데이터 흐름을 설명합니다.

---

## System Overview (시스템 개요)

```
┌─────────────────────────────────────────────────────────────────┐
│                    GitHub Enterprise Server                      │
│                                                                 │
│  ┌──────────┐   label/comment   ┌──────────────────┐            │
│  │  Issue /  │ ─────────────────▶│  GitHub Actions  │            │
│  │    PR     │                   │   (Workflow)     │            │
│  └──────────┘                   └────────┬─────────┘            │
│                                          │                      │
│                                          ▼                      │
│                                 ┌────────────────┐              │
│                                 │  Self-Hosted    │              │
│                                 │    Runner       │              │
│                                 └────────┬───────┘              │
└──────────────────────────────────────────┼──────────────────────┘
                                           │
                                           ▼
                              ┌─────────────────────┐
                              │    Orchestrator      │
                              │  (agent/orchestrator)│
                              └──────────┬──────────┘
                                         │
                        ┌────────────────┼────────────────┐
                        ▼                                ▼
                ┌──────────────┐                ┌──────────────┐
                │ Label Trigger│                │  CI Trigger  │
                │              │                │  (workflow_  │
                │ copilot      │                │   run)       │
                │ copilot-     │                │              │
                │  review      │                │              │
                │ copilot-docs │                │              │
                │ copilot-fix  │                │              │
                └──────┬───────┘                └──────┬───────┘
                       │                        │
                       └────────────┼────────────┘
                                        │
                                        ▼
                              ┌──────────────────┐
                              │   Agent Router   │
                              └──────────┬───────┘
                                         │
                  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
                  │  Coder   │ │ Reviewer │ │ Doc Gen  │ │  CI Fix  │
                  │  Agent   │ │  Agent   │ │  Agent   │ │  Agent   │
                  └──────────┘ └──────────┘ └──────────┘ └──────────┘
                                         │
                         ┌───────────────┼───────────────┐
                         ▼               ▼               ▼
           ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
           │ Copilot SDK  │ │  GHES REST   │ │    Tools     │
           │ / CLI        │ │   Client     │ │    (git)     │
           └──────┬───────┘ └──────┬───────┘ └──────────────┘
                  │                │
                  ▼                ▼
           ┌──────────────┐ ┌──────────────┐
           │  Copilot     │ │   GHES API   │
           │  Cloud API   │ │  (api/v3)    │
           └──────────────┘ └──────────────┘
```

---

## Component Descriptions (컴포넌트 설명)

### Orchestrator (`agent/orchestrator.py`)

시스템의 진입점이자 라우터입니다.

- GitHub Actions event payload를 수신
- Trigger를 파싱하여 적절한 Agent로 라우팅
- 에이전트 실행 lifecycle 관리 (시작 → 진행 → 완료/에러)
- Issue/PR에 진행 상황 코멘트 게시

```python
class Orchestrator:
    """Routes GitHub Actions events to the appropriate agent."""
    async def run(self, event_path: str) -> None: ...
    async def _route_agent(self, ctx: TriggerContext) -> str: ...
```

### Triggers (`agent/triggers/`)

GitHub webhook 이벤트를 파싱하여 `TriggerContext`를 생성합니다.

| Trigger | 파일 | 이벤트 |
|---------|------|--------|
| **LabelTrigger** | `label_trigger.py` | `issues.labeled`, `pull_request.labeled` |

#### TriggerContext (트리거 컨텍스트)

모든 에이전트에 전달되는 실행 컨텍스트:

```python
@dataclass
class TriggerContext:
    agent_type: AgentType     # CODER, REVIEWER, DOC_GEN, CI_FIX
    event_type: str           # "issues", "pull_request", "workflow_run"
    owner: str                # 리포지토리 소유자
    repo: str                 # 리포지토리 이름
    issue_number: int | None  # 이슈 번호
    pr_number: int | None     # PR 번호
    issue_title: str          # 이슈/PR 제목
    issue_body: str           # 이슈/PR 본문
    creator: str              # 이슈 생성자
    server_url: str           # GHES 서버 URL
    run_id: str | None        # GitHub Actions run ID
```

### Agents (`agent/agents/`)

각 에이전트는 독립적인 작업 단위를 수행합니다.

#### CoderAgent (`coder_agent.py`)

이슈를 분석하고 코드를 생성하여 PR을 여는 에이전트:

1. 이슈에 `in-progress` 레이블 추가
2. 리포지토리 컨텍스트 수집 (구조, 주요 파일)
3. 리포지토리 클론 및 브랜치 생성
4. Copilot SDK/CLI로 코드 생성
5. 커밋 및 푸시
6. git metadata를 기반으로 경량 모델이 PR 요약/검증 섹션 생성
7. 전체 파일 목록 대신 변경 규모와 Files changed 탭 안내를 포함해 PR 생성
8. 완료 레이블 업데이트

#### ReviewerAgent (`reviewer_agent.py`)

두 AI 모델을 같은 조건으로 병렬 실행하여 코드 리뷰를 교차검증:

1. PR diff 및 변경 파일 anchor 수집
2. Claude와 GPT-5.4에 동일한 diff, 변경 파일 anchor, 파일 컨텍스트, 공통 rubric 전달
3. 두 모델 모두 Copilot workspace tools로 관련 호출자, 테스트, 설정, 문서,
  public API 경계를 필요 시 확인하며 전체 범위를 리뷰
4. Claude는 보안/아키텍처/유지보수성,
   GPT-5.4는 정확성/성능/엣지케이스를 더 깊게 점검
5. 개별 리뷰 결과 코멘트 게시
6. 통합 요약에서 중복 제거, 합의/불일치 분석, 최종 판단 생성 및 게시
7. 합의되었거나 근거가 강한 항목 중 valid PR diff 라인에 해당하는 것만
  inline Suggested Changes로 생성 및 게시

#### DocGenAgent (`doc_gen_agent.py`)

문서 생성 및 업데이트를 수행합니다.

1. PR에서 실행되면 GHES API로 변경 파일 목록을 seed context로 수집
2. Issue에서 실행되면 전체 리포지토리 문서 점검 scope로 실행
3. 기존 주요 문서(`README.md`, `docs/README.md`, `docs/API.md` 등)를 seed로 수집
4. Copilot SDK/CLI 세션을 checked-out working tree에서 실행
5. 프롬프트에서 seed 파일을 anchor로 사용하고, 필요한 경우 workspace의 관련 코드,
   테스트, 설정, 문서를 file tools로 탐색하도록 지시
6. 문서 파일 또는 docstring 변경 후 외부 orchestration이 커밋, 푸시, PR 생성을 처리

#### CIFixAgent (`ci_fix_agent.py`)

CI 실패 로그와 실패 job 정보를 분석하여 자동 수정 커밋을 생성합니다.


### Copilot Session (`agent/copilot_session.py`)

Copilot SDK/CLI와의 인터페이스:

- **SDK 모드**: `github-copilot-sdk` 패키지 사용
- **CLI 폴백**: SDK 미설치 시 `copilot` CLI 사용
- **기능**: 타임아웃, 재시도 (최대 3회, 지수 백오프), 멀티모델 병렬 실행
- **Tool 등록**: 에이전트가 사용할 도구 등록 지원

```python
async with CopilotSessionManager(model="claude-sonnet-4.6") as session:
    result = await session.execute(prompt)
    
    # 멀티모델 병렬 실행
    results = await session.execute_parallel(
        prompt, models=["claude-opus-4.6", "gpt-5.4"]
    )
```

### GHES Client (`agent/ghes_client.py`)

GitHub Enterprise Server REST API 비동기 클라이언트:

- `httpx.AsyncClient` 기반
- 자동 재시도: 429 (Rate Limit) + 5xx (서버 에러) → 최대 5회, 지수 백오프
- Bearer 토큰 인증 (Classic PAT)
- github.com / GHES 양쪽 URL 패턴 지원

### Tools (`agent/tools/`)

에이전트가 사용하는 도구 모음:

| 도구 | 파일 | 설명 |
|------|------|------|
| **Git Operations** | `git_tools.py` | branch, commit, push, diff 등 |

### Config (`agent/config.py`)

`pydantic-settings` 기반 설정 관리:

```
AppConfig
├── GHESConfig          # 서버 URL, 호스트, GH_TOKEN, COPILOT_GITHUB_TOKEN
├── CopilotConfig       # 모델, CLI 버전
└── AgentConfig         # 타임아웃, 재시도, 브랜치 접두사, 출력 언어
```

### Prompt Templates (`agent/utils/prompts.py`)

Jinja2 기반 프롬프트 템플릿 시스템:

- `PromptManager`를 통한 템플릿 등록/렌더링
- 에이전트별 전용 프롬프트 (Coder, Reviewer, CI Fix, Doc Gen)
- 커스텀 템플릿 추가 가능

---

## Data Flow Diagrams (데이터 흐름)

### Issue → Agent → PR (코더 에이전트)

```
 사용자                 GHES                  Runner               Copilot Cloud
   │                     │                      │                       │
   │  1. Issue 생성       │                      │                       │
   │  + copilot 레이블    │                      │                       │
   │ ───────────────────▶│                      │                       │
   │                     │  2. Webhook 발생      │                       │
   │                     │ ────────────────────▶ │                       │
   │                     │                      │  3. 이벤트 파싱         │
   │                     │                      │     Trigger 매칭       │
   │                     │                      │                       │
   │                     │◀──── 4. "Working..." │                       │
   │                     │         코멘트        │                       │
   │                     │                      │                       │
   │                     │◀──── 5. 리포지토리     │                       │
   │                     │         컨텍스트 수집   │                       │
   │                     │                      │                       │
   │                     │                      │  6. 코드 생성 요청       │
   │                     │                      │ ─────────────────────▶│
   │                     │                      │                       │
   │                     │                      │◀─ 7. 생성된 코드        │
   │                     │                      │                       │
   │                     │◀──── 8. Branch push   │                       │
   │                     │                      │                       │
   │                     │◀──── 9. PR 생성      │                       │
   │                     │                      │                       │
   │◀─── 10. PR 알림 ───│                      │                       │
   │                     │                      │                       │
```

### Multi-Model Review (리뷰어 에이전트)

```
                          ┌─────────────────┐
                          │   PR + Diff     │
                          └────────┬────────┘
                                   │
                          ┌────────▼────────┐
                          │  ReviewerAgent  │
                          └────────┬────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
           ┌────────▼────────┐           ┌────────▼────────┐
           │  Claude Session │           │  GPT-5.4 Session  │
           │                 │           │                 │
           │  Security     │           │  Bugs         │
           │  Architecture │           │  Performance   │
           │  Design       │           │  Edge Cases   │
           │  Maintain.    │           │  Error Hdl.   │
           └────────┬────────┘           └────────┬────────┘
                    │                             │
                    └──────────────┬──────────────┘
                                   │
                          ┌────────▼────────┐
                          │  Consolidated   │
                          │    Summary      │
                          │                 │
                          │  Consensus    │
                          │  Key Findings │
                          │ ✅ Final Verdict│
                          └────────┬────────┘
                                   │
                          ┌────────▼────────┐
                          │ Inline Suggested│
                          │    Changes      │
                          │ EXPLANATION text│
                          └─────────────────┘
```

---

## Security Considerations (보안 고려사항)

### 인증

- **두 개의 자격증명 체계**: GHES API와 Copilot SDK는 별도의 토큰을 사용할 수 있습니다
  - `GH_TOKEN`: GHES 인스턴스 PAT — GHES API 호출 (이슈, PR, git)에 사용
  - `COPILOT_GITHUB_TOKEN` (옵션): Copilot SDK 전용 토큰 — 미설정 시 runner의 `copilot login` 자격증명 사용
- `GH_TOKEN`은 Copilot 인증에 사용하지 않습니다.
- 토큰은 환경변수/시크릿으로만 주입 (하드코딩 절대 금지)
- Runner에서 토큰은 `git credential store`에 임시 저장

### 입력 검증

- 모든 사용자 입력 (이슈 본문, 코멘트 등)은 `pydantic`으로 검증
- 파일 경로에 사용자 입력 직접 사용 금지 (path traversal 방지)
- Markdown 출력에서 injection 방지

### 네트워크 보안

- GHES API: HTTPS only
- Copilot API: TLS 1.2+ 필수
- Runner outbound 네트워크 정책에서 GHES API와 Copilot API 엔드포인트 접근을 허용해야 합니다.

### 코드 실행 격리

- 에이전트 생성 코드는 self-hosted runner VM 내에서만 실행
- 프로덕션 시크릿은 runner에 노출하지 않음

---

## Scalability & Concurrency (스케일링 및 동시성)

### 동시성 제어

대규모 (1000+ 리포지토리) 환경에서 Copilot API 과부하를 방지하기 위해 두 단계의 동시성 제어를 적용합니다.

#### 1. Copilot 세션 세마포어 (프로세스 내)

`agent/copilot_session.py`에서 `asyncio.Semaphore`를 사용하여 **단일 runner 프로세스 내** Copilot API 동시 호출을 제한합니다.

```python
MAX_CONCURRENT_SESSIONS = 5
_copilot_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
```

- 기본값: **5** (코드 상수)
- Reviewer 에이전트의 멀티모델 병렬 실행 등에서 동시 세션 수를 제한
- 세마포어 획득 대기 중인 요청은 asyncio 이벤트 루프에서 논블로킹으로 큐잉

#### 2. GitHub Actions 동시성 그룹 (워크플로우 간)

각 워크플로우에 `concurrency` 그룹을 설정하여 **동일 이슈/PR에 대한 중복 실행을 방지**합니다.

```yaml
# copilot-coder-master.yml
concurrency:
  group: copilot-coder-${{ github.event.issue.number }}
  cancel-in-progress: true
```

| 워크플로우 | 동시성 그룹 키 | cancel-in-progress |
|-----------|--------------|-------------------|
| Coder | `copilot-coder-{issue_number}` | `true` |
| Reviewer | `copilot-reviewer-{pr_number}` | `true` |
| CI Fix | `ci-fix-{workflow_run_id}` | `true` |

### 큐잉 / 백프레셔

현재는 GitHub Actions의 내장 큐잉 메커니즘에 의존합니다:

- **동시성 그룹**: 동일 그룹 내 후속 실행은 진행 중인 실행 완료 후 자동 시작
- **Runner 큐**: 사용 가능한 runner가 없으면 Actions가 자동으로 대기열에 추가
- **Rate limit 재시도**: `GHESClient`가 429 응답 시 자동 지수 백오프 (최대 5회)

> **향후 확장**: 1000+ 리포지토리에서 동시 트리거가 폭주하는 경우, 외부 메시지 큐 (Redis Queue, AWS SQS 등)를 도입하여 orchestrator 앞단에서 요청을 버퍼링하고 처리 속도를 조절할 수 있습니다. 현재 아키텍처는 orchestrator가 stateless이므로 큐 소비자로 전환이 용이합니다.

### 장애 도메인 (Failure Domain)

에이전트 실행 실패 시 에러 전파 및 격리 방식:

```
Agent 실행 실패
    │
    ├── 1. except Exception 캐치 (orchestrator.py)
    │       └── traceback을 structlog로 기록
    │
    ├── 2. _post_error() 호출
    │       └── 해당 이슈/PR에 에러 코멘트 게시
    │           (에러 메시지 최대 3000자 포함)
    │
    └── 3. 프로세스 종료 (exit code 1)
            └── GitHub Actions에서 해당 job만 실패로 표시
```

- **이슈/PR 단위 격리**: 하나의 에이전트 실패가 다른 이슈/PR의 에이전트 실행에 영향을 주지 않음
- **에러 가시성**: 실패 시 이슈/PR에 자동으로 에러 코멘트가 게시되어 사용자에게 즉시 통지
- **Actions 로그**: 전체 스택 트레이스는 GitHub Actions 로그에서 확인 가능

### 레포별 설정 (Per-Repository Configuration)

각 대상 리포지토리에서 `.github/ghes-agent.yml` 파일을 통해 에이전트 동작을 오버라이드할 수 있습니다.

```yaml
# .github/ghes-agent.yml
default_branch: develop          # PR 타겟 브랜치 (기본: main)
timeout_minutes: 45              # 에이전트 타임아웃 (기본: 30)
max_retries: 5                   # 재시도 횟수 (기본: 3)
output_language: ko              # 리뷰/PR/요약/EXPLANATION 출력 언어
branch_prefix: copilot/          # 에이전트 브랜치 접두사
coder_model: claude-sonnet-4.6   # 코드 생성 모델
coder_pr_summary_model: gpt-5.4-mini   # PR 본문 요약용 경량 모델
reviewer_models:                 # 병렬 리뷰 모델
  - claude-opus-4.6
  - gpt-5.4
reviewer_summary_model: claude-opus-4.6      # 통합 요약 모델
reviewer_suggestion_model: claude-opus-4.6   # inline suggestion 변환 모델
```

- Orchestrator가 실행 시 대상 리포지토리의 `.github/ghes-agent.yml`을 읽어 기본 설정을 오버라이드
- 파일이 없으면 글로벌 기본값 (`agent/config.py`의 `AgentConfig`) 적용
- 리포지토리별로 브랜치 전략, 타임아웃, 출력 언어, 모델을 독립적으로 관리 가능

---

## Extension Points (확장 포인트)

### 새 에이전트 추가

1. `agent/agents/` 디렉토리에 새 에이전트 클래스 생성
2. `AgentType` enum에 새 타입 추가
3. `Orchestrator._route_agent()`에 라우팅 추가
4. `LabelTrigger.LABEL_MAP`에 트리거 레이블 추가

```python
# agent/agents/security_audit.py
class SecurityAuditAgent:
    async def execute(self, ctx, ghes_client, config) -> str:
        ...
```

### 새 Tool 추가

1. `agent/tools/` 디렉토리에 도구 모듈 생성
2. Copilot session에 등록하여 사용

### 프롬프트 커스터마이징

```python
pm = PromptManager()
pm.register("custom_review", "Your custom prompt template {{ variable }}")
```

## 관련 문서

- [SETUP.md](./SETUP.md) — 설치 가이드
- [README.md](../README.md) — 프로젝트 개요
