# Copilot Instructions — GHES Coding Agent

> 이 문서는 Copilot CLI가 코드를 생성할 때 따라야 할 규칙과 컨벤션을 정의합니다.

---

## 1. Project Overview (프로젝트 개요)

- **Python 3.11+** 기반의 autonomous coding agent
- GitHub Enterprise Server (GHES) 환경에서 동작하며, Copilot SDK를 활용
- **async-first** 아키텍처: 모든 I/O는 비동기로 처리
- 주요 의존성: `copilot-sdk`, `httpx`, `pydantic`, `structlog`, `pytest-asyncio`

---

## 2. Code Style (코드 스타일)

### async/await

- 모든 I/O 작업(API 호출, 파일 읽기/쓰기, DB 접근)은 반드시 `async/await`를 사용한다.
- 동기 함수에서 비동기 함수를 호출하지 않는다. 필요시 `asyncio.run()`은 entrypoint에서만 사용.

```python
# ✅ Good
async def fetch_issue(client: GHESClient, number: int) -> Issue:
    return await client.get(f"/repos/{owner}/{repo}/issues/{number}")

# ❌ Bad — 동기 함수에서 비동기 호출
def fetch_issue(client: GHESClient, number: int) -> Issue:
    return asyncio.run(client.get(...))
```

### Type Hints (타입 힌트)

- 모든 함수의 파라미터와 리턴 타입에 type hint를 명시한다.
- `Any` 타입은 최대한 피하고, 구체적인 타입이나 `Protocol`을 사용한다.
- 컬렉션은 `list[str]`, `dict[str, int]` 등 built-in generic을 사용한다 (Python 3.9+ 스타일).

```python
# ✅ Good
async def list_files(repo: str, path: str = "/") -> list[FileEntry]:
    ...

# ❌ Bad — type hint 누락
async def list_files(repo, path="/"):
    ...
```

### Docstrings

- Google style docstring을 사용한다.
- 모든 public 클래스와 함수에 docstring을 작성한다.

```python
async def create_branch(client: GHESClient, repo: str, branch: str, sha: str) -> BranchRef:
    """GHES에서 새 브랜치를 생성한다.

    Args:
        client: GHES API 클라이언트.
        repo: 'owner/repo' 형식의 리포지토리 이름.
        branch: 생성할 브랜치 이름 (refs/heads/ 접두사 없이).
        sha: 브랜치의 base commit SHA.

    Returns:
        생성된 브랜치의 reference 객체.

    Raises:
        GHESAPIError: API 호출 실패 시.
    """
```

### Formatting (포맷팅)

- **Max line length**: 100자
- Formatter: `black` (line-length=100)
- Import 정렬: `isort` (profile=black)
- Linter: `ruff`

### Logging (로깅)

- `structlog`를 사용하여 structured logging을 한다.
- `print()` 문은 사용하지 않는다.
- 로그 레벨: DEBUG(개발), INFO(일반 흐름), WARNING(복구 가능), ERROR(실패)

```python
import structlog

logger = structlog.get_logger(__name__)

async def process_event(event: WebhookEvent) -> None:
    logger.info("processing_event", event_type=event.action, issue=event.issue.number)
```

### Data Validation (데이터 검증)

- 외부 입력(API 응답, webhook payload 등)은 `pydantic` 모델로 파싱/검증한다.
- 설정값은 `pydantic-settings`의 `BaseSettings`를 사용한다.

```python
from pydantic import BaseModel, Field

class IssueComment(BaseModel):
    id: int
    body: str
    user: User
    created_at: datetime = Field(description="코멘트 생성 시각")
```

---

## 3. Architecture Rules (아키텍처 규칙)

### Agent 패턴

- 각 agent는 독립된 클래스로 구현하며, 반드시 `async def execute(self, context: AgentContext)` 메서드를 가진다.
- Agent는 상태를 갖지 않는다 (stateless). 실행에 필요한 모든 정보는 `context`로 전달.

```python
class CodeFixAgent:
    """코드 수정을 수행하는 agent."""

    async def execute(self, context: AgentContext) -> AgentResult:
        """Issue에 기술된 문제를 분석하고 코드를 수정한다."""
        issue = await context.ghes_client.get_issue(context.repo, context.issue_number)
        # ... agent 로직
        return AgentResult(status="success", changes=changes)
```

### Tool 등록

- Tool은 Copilot SDK session에 등록하여 agent가 사용할 수 있게 한다.
- Tool 함수는 명확한 이름과 description을 가져야 한다.

```python
@session.tool(description="GHES 리포지토리에서 파일 내용을 읽는다")
async def read_file(repo: str, path: str, ref: str = "main") -> str:
    ...
```

### GHES API 클라이언트

- 모든 GHES API 호출은 `GHESClient`를 통해 수행한다. 직접 `httpx`를 호출하지 않는다.
- `GHESClient`는 자동 retry, rate limit 처리, 에러 핸들링을 담당한다.

```python
class GHESClient:
    """GHES REST/GraphQL API 클라이언트.

    모든 API 호출의 single entry point. retry와 rate limit을 자동 처리한다.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v3",
            headers={"Authorization": f"token {token}"},
        )
```

### Config 관리

- 환경변수를 통한 설정은 `pydantic-settings`로 관리한다.
- `.env` 파일 지원하되, 환경변수가 우선한다.

```python
from pydantic_settings import BaseSettings

class AgentConfig(BaseSettings):
    ghes_url: str = Field(description="GHES 인스턴스 URL (예: https://github.example.com)")
    ghes_token: str = Field(description="GHES PAT (GH_TOKEN — GHES API 호출 전용)")
    copilot_github_token: str | None = Field(default=None, description="Copilot SDK 인증용 토큰")
    log_level: str = Field(default="INFO")

    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env")
```

---

## 4. Git Conventions (Git 컨벤션)

### Commit Messages

- **Conventional Commits** 형식을 따른다.
- 타입: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`
- scope는 모듈 이름을 사용한다.

```
feat(agent): issue 분석 로직 추가
fix(ghes-client): rate limit retry 로직 수정
docs(readme): 설치 가이드 업데이트
test(tools): read_file tool 단위 테스트 추가
```

### Co-authored-by

- 모든 commit에 Co-authored-by trailer를 포함한다.

```
feat(agent): implement code review agent

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

### Branch Naming (브랜치 이름)

- `copilot/{issue-number}` 형식을 사용한다.
- 예: `copilot/42`, `copilot/108`

---

## 5. Security Rules (보안 규칙)

### 토큰/시크릿 관리

- **절대로** 토큰, API 키, 비밀번호를 코드에 하드코딩하지 않는다.
- 환경변수 또는 시크릿 매니저를 통해서만 주입한다.
- `.env` 파일은 반드시 `.gitignore`에 포함되어야 한다.

### 입력 검증

- Issue body, comment body 등 사용자 입력은 반드시 검증/sanitize한다.
- 특히 코드 실행이나 파일 경로에 사용자 입력을 직접 사용하지 않는다.

```python
# ✅ Good — pydantic으로 검증
class AgentCommand(BaseModel):
    action: Literal["fix", "review", "explain"]
    target_file: str = Field(pattern=r"^[a-zA-Z0-9_/.\-]+$")

# ❌ Bad — 사용자 입력을 그대로 사용
file_path = comment.body.split("fix ")[1]  # 위험: path traversal 가능
```

### API 호출 안전성

- API 호출 시 string interpolation 대신 parameterized 방식을 사용한다.
- URL path는 반드시 encoding하여 injection을 방지한다.

```python
# ✅ Good — 파라미터화된 경로
await client.get(f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues/{number}")

# ❌ Bad — 검증 없이 직접 삽입
await client.get(f"/repos/{user_input}/issues/{number}")
```

### Markdown 출력 Sanitize

- Agent가 생성하는 markdown 출력(PR body, comment 등)에서 injection을 방지한다.
- 사용자 입력이 포함될 경우 code fence 내부에 넣거나 escape 처리한다.

---

## 6. Testing (테스트)

### 프레임워크

- `pytest` + `pytest-asyncio`를 사용한다.
- 비동기 테스트는 `@pytest.mark.asyncio` 데코레이터를 사용한다.

### 파일 구조

- 테스트 파일: `tests/test_{module}.py`
- fixture 파일: `tests/conftest.py`
- 테스트 데이터: `tests/fixtures/`

### 외부 API Mock

- 외부 API 호출은 반드시 mock하고, 실제 네트워크 요청을 보내지 않는다.
- `pytest-httpx` 또는 `respx`를 사용하여 httpx 요청을 mock한다.

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_fetch_issue(mock_ghes_client: AsyncMock) -> None:
    """Issue를 정상적으로 가져오는지 테스트한다."""
    mock_ghes_client.get_issue.return_value = Issue(
        number=42,
        title="Bug: login fails",
        body="로그인 시 500 에러 발생",
    )

    agent = CodeFixAgent()
    result = await agent.execute(context)

    assert result.status == "success"
    mock_ghes_client.get_issue.assert_awaited_once_with("owner/repo", 42)
```

### 커버리지

- 새로운 모듈 추가 시 최소한의 happy path + error case 테스트를 작성한다.
- `pytest-cov`로 커버리지를 측정한다.

---

## 7. GHES Specific (GHES 관련 규칙)

### API Base URL

- `api.github.com`을 하드코딩하지 않는다.
- GHES 인스턴스의 API 경로: `https://{ghes-host}/api/v3/...`
- GraphQL endpoint: `https://{ghes-host}/api/graphql`

```python
# ✅ Good — 설정에서 base URL을 읽어 사용
client = GHESClient(base_url=config.ghes_url, token=config.ghes_token)

# ❌ Bad — github.com 하드코딩
client = httpx.AsyncClient(base_url="https://api.github.com")
```

### Authentication (인증)

- `GH_TOKEN`: GHES API와 git 인증 전용으로 사용한다.
- `COPILOT_GITHUB_TOKEN` (옵션): Copilot SDK 인증 전용으로 사용한다. 미설정 시 self-hosted runner에서 `copilot login`으로 저장한 OAuth 자격증명을 사용한다.
- `GH_TOKEN`을 Copilot 인증 토큰으로 취급하지 않는다.
- GHES API 토큰 scope 최소 권한: `repo`, `read:org`. `.github/workflows/*` 파일을 생성/수정하는 설치 작업이나 agent 작업에는 `workflow` scope를 추가한다.

### URL 패턴 호환

- github.com과 GHES 양쪽 URL 패턴을 모두 처리할 수 있어야 한다.
- URL 파싱 시 하드코딩된 도메인이 아닌 설정 기반으로 판별한다.

```python
def parse_repo_url(url: str, ghes_host: str) -> tuple[str, str]:
    """github.com 또는 GHES URL에서 owner/repo를 추출한다.

    Args:
        url: 리포지토리 URL.
        ghes_host: GHES 인스턴스 호스트명.

    Returns:
        (owner, repo) 튜플.
    """
    parsed = urlparse(url)
    if parsed.hostname in ("github.com", ghes_host):
        parts = parsed.path.strip("/").split("/")
        return parts[0], parts[1].removesuffix(".git")
    raise ValueError(f"Unsupported host: {parsed.hostname}")
```

### GHES 버전 호환

- GHES API 호출 시 버전별 차이를 고려한다.
- API 기능 사용 전 GHES 버전 확인이 필요한 경우 `/meta` endpoint를 활용한다.

---

## 8. Quick Reference (빠른 참조)

| 항목 | 규칙 |
|------|------|
| Python 버전 | 3.11+ |
| 비동기 | `async/await` 필수 |
| 타입 힌트 | 모든 함수에 명시 |
| Docstring | Google style |
| Line length | 100자 |
| Formatter | `black` (line-length=100) |
| Linter | `ruff` |
| Logging | `structlog` (no print) |
| Validation | `pydantic` |
| Config | `pydantic-settings` + 환경변수 |
| Test | `pytest` + `pytest-asyncio` |
| Commit | Conventional Commits |
| Branch | `copilot/{issue-number}` |
| API Client | `GHESClient` 경유 필수 |
| Token | `GH_TOKEN` (GHES API/git), `COPILOT_GITHUB_TOKEN` 또는 `copilot login` (Copilot SDK) |
