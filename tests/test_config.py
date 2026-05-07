"""Tests for configuration loading and derivation logic."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import httpx
from pytest import MonkeyPatch

import agent.config as config_module
from agent.config import (
    AgentConfig,
    AppConfig,
    CopilotConfig,
    GHESConfig,
    load_repo_config,
)


class TestGHESConfig:
    def test_ghes_config_from_env(self):
        """Hostname is extracted from GITHUB_SERVER_URL."""
        with patch.dict(os.environ, {"GITHUB_SERVER_URL": "https://ghes.example.com"}):
            cfg = GHESConfig()
        assert cfg.hostname == "ghes.example.com"

    def test_ghes_api_base_github_com(self):
        """github.com maps to api.github.com."""
        with patch.dict(os.environ, {"GITHUB_SERVER_URL": "https://github.com"}):
            cfg = GHESConfig()
        assert cfg.api_base_url == "https://api.github.com"

    def test_ghes_api_base_enterprise(self):
        """Enterprise hostname maps to /api/v3."""
        with patch.dict(os.environ, {"GITHUB_SERVER_URL": "https://ghes.example.com"}):
            cfg = GHESConfig()
        assert cfg.api_base_url == "https://ghes.example.com/api/v3"


class TestAppConfig:
    def test_app_config_defaults(self):
        """Default values are sensible."""
        cfg = AppConfig()
        assert cfg.agent.timeout_minutes == 30
        assert cfg.agent.max_retries == 3
        assert cfg.agent.branch_prefix == "copilot/"
        assert cfg.copilot.coder_model == "claude-sonnet-4.6"
        assert cfg.copilot.coder_pr_summary_model == "gpt-5.4-mini"

    def test_agent_output_language_can_be_populated_by_field_name(self):
        """Direct test construction can use the Python field name."""
        cfg = AppConfig(agent=AgentConfig(output_language="ko"))
        assert cfg.agent.output_language == "ko"

    def test_copilot_github_token_default_none(self):
        """COPILOT_GITHUB_TOKEN defaults to None when not set."""
        with patch.dict(os.environ, {}, clear=False):
            env = os.environ.copy()
            env.pop("COPILOT_GITHUB_TOKEN", None)
            with patch.dict(os.environ, env, clear=True):
                cfg = GHESConfig()
        assert cfg.copilot_github_token is None

    def test_copilot_github_token_from_env(self):
        """COPILOT_GITHUB_TOKEN is loaded from environment."""
        with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "ghp_copilot_test_token"}):
            cfg = GHESConfig()
        assert cfg.copilot_github_token == "ghp_copilot_test_token"

    def test_gh_token_and_copilot_token_independent(self):
        """GH_TOKEN and COPILOT_GITHUB_TOKEN are independent fields."""
        with patch.dict(os.environ, {
            "GH_TOKEN": "ghp_ghes_token",
            "COPILOT_GITHUB_TOKEN": "ghp_copilot_token",
        }):
            cfg = GHESConfig()
        assert cfg.gh_token == "ghp_ghes_token"
        assert cfg.copilot_github_token == "ghp_copilot_token"

    def test_reviewer_orchestration_models_default_from_reviewer_models(self):
        """Summary and suggestion models default from the reviewer model list."""
        cfg = CopilotConfig(reviewer_models=["claude-opus-4.6", "gpt-5.4"])

        assert cfg.reviewer_summary_model == "claude-opus-4.6"
        assert cfg.reviewer_suggestion_model == "claude-opus-4.6"

    def test_reviewer_suggestion_model_defaults_from_summary_model(self):
        """Suggestion model follows an explicit summary model when omitted."""
        cfg = CopilotConfig(
            reviewer_models=["claude-opus-4.6", "gpt-5.4"],
            reviewer_summary_model="gpt-5.4",
        )

        assert cfg.reviewer_summary_model == "gpt-5.4"
        assert cfg.reviewer_suggestion_model == "gpt-5.4"


class TestRepoConfig:
    async def test_load_repo_config_merges_allowed_overrides(
        self, monkeypatch: MonkeyPatch,
    ) -> None:
        """Repository YAML overrides documented settings."""

        class FakeYaml:
            class YAMLError(Exception):
                pass

            @staticmethod
            def safe_load(raw: str) -> dict[str, object]:
                return {
                    "default_branch": "develop",
                    "output_language": "ko",
                    "coder_pr_summary_model": "gpt-5.4-mini",
                    "reviewer_models": ["claude-opus-4.6", "gpt-5.4"],
                    "reviewer_summary_model": "claude-opus-4.6",
                    "reviewer_suggestion_model": "gpt-5.4",
                    "ignored_key": "ignored",
                }

        monkeypatch.setattr(config_module, "yaml", FakeYaml)
        client = AsyncMock()
        client.get_file_content.return_value = "output_language: ko"
        global_config = AppConfig()

        merged = await load_repo_config(client, "acme/webapp", global_config)

        assert merged is not global_config
        assert merged.agent.default_branch == "develop"
        assert merged.agent.output_language == "ko"
        assert merged.copilot.coder_pr_summary_model == "gpt-5.4-mini"
        assert merged.copilot.reviewer_models == ["claude-opus-4.6", "gpt-5.4"]
        assert merged.copilot.reviewer_summary_model == "claude-opus-4.6"
        assert merged.copilot.reviewer_suggestion_model == "gpt-5.4"
        assert not hasattr(merged.agent, "ignored_key")

    async def test_load_repo_config_derives_reviewer_orchestration_models(
        self, monkeypatch: MonkeyPatch,
    ) -> None:
        """Repo reviewer_models overrides also update derived reviewer models."""

        class FakeYaml:
            class YAMLError(Exception):
                pass

            @staticmethod
            def safe_load(raw: str) -> dict[str, object]:
                return {"reviewer_models": ["gpt-5.4", "claude-opus-4.6"]}

        monkeypatch.setattr(config_module, "yaml", FakeYaml)
        client = AsyncMock()
        client.get_file_content.return_value = "reviewer_models: [gpt-5.4]"
        global_config = AppConfig()

        merged = await load_repo_config(client, "acme/webapp", global_config)

        assert merged.copilot.reviewer_models == ["gpt-5.4", "claude-opus-4.6"]
        assert merged.copilot.reviewer_summary_model == "gpt-5.4"
        assert merged.copilot.reviewer_suggestion_model == "gpt-5.4"

    async def test_load_repo_config_returns_global_config_when_missing(self) -> None:
        """Missing repository YAML leaves global settings untouched."""
        request = httpx.Request("GET", "https://github.example.com/config")
        response = httpx.Response(404, request=request)
        client = AsyncMock()
        client.get_file_content.side_effect = httpx.HTTPStatusError(
            "not found", request=request, response=response,
        )
        global_config = AppConfig()

        merged = await load_repo_config(client, "acme/webapp", global_config)

        assert merged is global_config
