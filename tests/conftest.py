"""Shared fixtures for the GHES Coding Agent test suite."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.config import AgentConfig, AppConfig, CopilotConfig, GHESConfig
from agent.ghes_client import GHESClient

# ---------------------------------------------------------------------------
# GitHub Actions event payloads
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_issue_event() -> dict:
    """GitHub Actions event payload for issues.labeled with 'copilot' label."""
    return {
        "action": "labeled",
        "label": {"name": "copilot"},
        "issue": {
            "number": 42,
            "title": "Add user authentication",
            "body": "We need JWT-based auth for the API.",
            "user": {"login": "testuser"},
            "html_url": "https://github.example.com/acme/webapp/issues/42",
            "repository_url": "https://github.example.com/repos/acme/webapp",
        },
    }


@pytest.fixture
def sample_pr_event() -> dict:
    """GitHub Actions event payload for pull_request.labeled."""
    return {
        "action": "labeled",
        "label": {"name": "copilot-review"},
        "pull_request": {
            "number": 99,
            "title": "feat: add auth middleware",
            "body": "Implements JWT auth middleware.",
            "user": {"login": "prauthor"},
            "html_url": "https://github.example.com/acme/webapp/pull/99",
            "repository_url": "https://github.example.com/repos/acme/webapp",
        },
    }


# ---------------------------------------------------------------------------
# Mocked dependencies
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ghes_client() -> MagicMock:
    """A mocked GHESClient with async methods."""
    client = MagicMock(spec=GHESClient)
    client.host = "github.example.com"
    client.token = "ghp_test_token"
    client.api_base = "https://github.example.com/api/v3"
    client.create_issue_comment = AsyncMock(return_value={"id": 1})
    client.get_issue = AsyncMock(return_value={"number": 42, "title": "Test"})
    client.create_pull_request = AsyncMock(return_value={"number": 100})
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_config() -> AppConfig:
    """AppConfig with test values (no real env vars needed)."""
    return AppConfig(
        ghes=GHESConfig(
            server_url="https://github.example.com",
            gh_token="ghp_test_token",
        ),
        copilot=CopilotConfig(),
        agent=AgentConfig(timeout_minutes=5, max_retries=1),
    )
