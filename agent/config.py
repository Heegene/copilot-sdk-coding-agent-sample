"""Configuration management for the GHES Coding Agent.

All settings are loaded from environment variables (or a .env file).
Use ``AppConfig.from_env()`` as the single entry-point.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
import structlog
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# GHES connection
# ---------------------------------------------------------------------------

class GHESConfig(BaseSettings):
    """GitHub Enterprise Server connection settings."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    server_url: str = Field(
        default="https://github.com",
        alias="GITHUB_SERVER_URL",
        description="Full URL of the GHES instance (e.g. https://github.example.com).",
    )
    hostname: str = Field(
        default="",
        description="Auto-detected hostname stripped from server_url.",
    )
    api_base_url: str = Field(
        default="",
        description="REST API base URL, auto-computed from server_url.",
    )
    gh_token: str = Field(
        default="",
        alias="GH_TOKEN",
        description=(
            "Personal Access Token for GHES API calls (issues, PRs, git). "
            "This is a PAT from the GHES instance (e.g. ghes.example.com/settings/tokens). "
            "Classic PATs (ghp_) work; fine-grained PATs (github_pat_) may also "
            "be used when supported by the GHES version. This token is not used "
            "for Copilot authentication."
        ),
    )
    copilot_github_token: str | None = Field(
        default=None,
        alias="COPILOT_GITHUB_TOKEN",
        description=(
            "Optional. If set, used for Copilot SDK auth. If not set, the "
            "runner must already be authenticated with 'copilot login'. "
            "GH_TOKEN is reserved for GHES API calls and is not a Copilot "
            "credential."
        ),
    )

    @model_validator(mode="after")
    def _compute_derived(self) -> GHESConfig:
        # Derive hostname from server_url
        url = self.server_url.rstrip("/")
        self.hostname = url.replace("https://", "").replace("http://", "")

        # Derive API base URL
        if self.hostname in ("github.com", "www.github.com"):
            self.api_base_url = "https://api.github.com"
        else:
            self.api_base_url = f"{url}/api/v3"
        return self


# ---------------------------------------------------------------------------
# Copilot SDK / model settings
# ---------------------------------------------------------------------------

class CopilotConfig(BaseSettings):
    """Settings for the Copilot SDK integration.

    Authentication
    --------------
    The Copilot SDK/CLI and the GHES API use **separate** credentials:

    - ``GH_TOKEN``: GHES instance PAT — used for GHES API operations only.
    - ``COPILOT_GITHUB_TOKEN`` (optional): Explicit token for Copilot SDK auth.

        Copilot authentication must use ``COPILOT_GITHUB_TOKEN`` or stored OAuth
        credentials from ``copilot login``. ``GH_TOKEN`` is reserved for GHES API
        calls and is not treated as a Copilot credential.
    """

    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore")

    coder_model: str = Field(
        default="claude-sonnet-4.6",
        description="Default model used for code generation tasks.",
    )
    coder_pr_summary_model: str = Field(
        default="gpt-5.4-mini",
        description="Lightweight model used to summarize generated PR changes.",
    )
    reviewer_models: list[str] = Field(
        default=["claude-opus-4.6", "gpt-5.4"],
        description="Models used for code-review tasks.",
    )
    reviewer_summary_model: str = Field(
        default="",
        description=(
            "Model used to synthesize multi-model review findings. "
            "Defaults to the first reviewer model when unset."
        ),
    )
    reviewer_suggestion_model: str = Field(
        default="",
        description=(
            "Model used to format accepted findings as inline GitHub suggestions. "
            "Defaults to reviewer_summary_model when unset."
        ),
    )
    cli_version: str = Field(
        default="latest",
        description="Copilot CLI version to target.",
    )

    @model_validator(mode="after")
    def _set_reviewer_model_defaults(self) -> CopilotConfig:
        """Derive reviewer orchestration models from reviewer_models when omitted."""
        fallback_model = (
            self.reviewer_models[0] if self.reviewer_models else "claude-sonnet-4.5"
        )
        if not self.reviewer_summary_model:
            self.reviewer_summary_model = fallback_model
        if not self.reviewer_suggestion_model:
            self.reviewer_suggestion_model = self.reviewer_summary_model
        return self


# ---------------------------------------------------------------------------
# Agent behaviour
# ---------------------------------------------------------------------------

DEFAULT_LABELS: dict[str, str] = {
    "copilot:fix": "fix",
    "copilot:feature": "feature",
    "copilot:refactor": "refactor",
    "copilot:test": "test",
}


class AgentConfig(BaseSettings):
    """Operational parameters for the agent runtime."""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    timeout_minutes: int = Field(
        default=30,
        description="Maximum wall-clock time (minutes) per agent run.",
    )
    max_retries: int = Field(
        default=3,
        description="Maximum number of retries on transient failures.",
    )
    branch_prefix: str = Field(
        default="copilot/",
        description="Prefix applied to branches created by the agent.",
    )
    default_branch: str = Field(
        default="main",
        description="PR 생성 시 사용할 기본 브랜치 이름",
    )
    labels: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_LABELS),
        description="Mapping of trigger labels to agent task types.",
    )
    output_language: Literal["en", "ko"] = Field(
        default="en",
        alias="AGENT_OUTPUT_LANGUAGE",
        description=(
            "Language for agent-authored output (review bodies, PR descriptions, "
            "summary text). Code, identifiers, commit messages, file paths, and "
            "parser-required markers remain unchanged. Allowed: en, ko."
        ),
    )

# ---------------------------------------------------------------------------
# Top-level application config
# ---------------------------------------------------------------------------

class AppConfig(BaseSettings):
    """Aggregate configuration for the entire application.

    Instantiate via ``AppConfig.from_env()`` to build every sub-config
    from the current environment / .env file.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ghes: GHESConfig = Field(default_factory=GHESConfig)
    copilot: CopilotConfig = Field(default_factory=CopilotConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    @classmethod
    def from_env(cls) -> AppConfig:
        """Build the full configuration tree from environment variables."""
        return cls(
            ghes=GHESConfig(),
            copilot=CopilotConfig(),
            agent=AgentConfig(),
        )


# ---------------------------------------------------------------------------
# Per-repo config override
# ---------------------------------------------------------------------------

# Fields in .github/ghes-agent.yml that may override global config.
_REPO_OVERRIDABLE: dict[str, tuple[str, str]] = {
    # yaml_key -> (sub-config attr, field name)
    "default_branch": ("agent", "default_branch"),
    "branch_prefix": ("agent", "branch_prefix"),
    "timeout_minutes": ("agent", "timeout_minutes"),
    "max_retries": ("agent", "max_retries"),
    "output_language": ("agent", "output_language"),
    "coder_model": ("copilot", "coder_model"),
    "coder_pr_summary_model": ("copilot", "coder_pr_summary_model"),
    "reviewer_models": ("copilot", "reviewer_models"),
    "reviewer_summary_model": ("copilot", "reviewer_summary_model"),
    "reviewer_suggestion_model": ("copilot", "reviewer_suggestion_model"),
}

_REPO_CONFIG_PATH = ".github/ghes-agent.yml"


async def load_repo_config(
    client: Any,
    repo: str,
    global_config: AppConfig,
) -> AppConfig:
    """리포지토리의 ``.github/ghes-agent.yml`` 을 읽어 글로벌 설정에 머지한다.

    Args:
        client: ``GHESClient`` 인스턴스 (import cycle 방지를 위해 Any 타입 사용).
        repo: ``owner/repo`` 형식의 리포지토리 이름.
        global_config: 환경변수에서 로드된 기본 설정.

    Returns:
        머지된 ``AppConfig``. 파일이 없거나 파싱에 실패하면 *global_config* 를 그대로 반환.
    """
    owner, repo_name = repo.split("/", 1)

    # -- 1. fetch file --------------------------------------------------------
    try:
        raw_content: str = await client.get_file_content(
            owner, repo_name, _REPO_CONFIG_PATH
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.debug("repo_config.not_found", repo=repo)
            return global_config
        raise

    # -- 2. parse YAML --------------------------------------------------------
    if yaml is None:
        logger.warning(
            "repo_config.yaml_unavailable",
            detail="PyYAML is not installed; repo config will be ignored.",
        )
        return global_config

    try:
        data: dict[str, Any] = yaml.safe_load(raw_content) or {}
    except yaml.YAMLError as exc:
        logger.warning("repo_config.parse_error", repo=repo, error=str(exc))
        return global_config

    if not isinstance(data, dict):
        logger.warning("repo_config.invalid_format", repo=repo)
        return global_config

    # -- 3. merge overrides ---------------------------------------------------
    overrides: dict[str, dict[str, Any]] = {}
    for yaml_key, (section, field) in _REPO_OVERRIDABLE.items():
        if yaml_key in data:
            overrides.setdefault(section, {})[field] = data[yaml_key]

    if not overrides:
        return global_config

    merged = global_config.model_copy(deep=True)

    for section, fields in overrides.items():
        sub = getattr(merged, section)
        for field_name, value in fields.items():
            setattr(sub, field_name, value)
            logger.info(
                "repo_config.override",
                repo=repo,
                field=f"{section}.{field_name}",
                value=value,
            )

    copilot_overrides = overrides.get("copilot", {})
    if "reviewer_models" in copilot_overrides:
        fallback_model = (
            merged.copilot.reviewer_models[0]
            if merged.copilot.reviewer_models
            else "claude-sonnet-4.5"
        )
        if "reviewer_summary_model" not in copilot_overrides:
            merged.copilot.reviewer_summary_model = fallback_model
        if "reviewer_suggestion_model" not in copilot_overrides:
            merged.copilot.reviewer_suggestion_model = (
                merged.copilot.reviewer_summary_model
            )
    elif (
        "reviewer_summary_model" in copilot_overrides
        and "reviewer_suggestion_model" not in copilot_overrides
    ):
        merged.copilot.reviewer_suggestion_model = (
            merged.copilot.reviewer_summary_model
        )

    return merged
