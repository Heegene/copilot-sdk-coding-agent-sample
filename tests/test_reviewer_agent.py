"""Tests for ReviewerAgent orchestration behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import agent.agents.reviewer_agent as reviewer_module
from agent.agents.reviewer_agent import ReviewerAgent
from agent.config import AppConfig, CopilotConfig


class _FakeCopilotSessionManager:
    """Minimal async context manager that captures requested model names."""

    models: list[str] = []

    def __init__(self, *, model: str, timeout: int) -> None:
        self.model = model
        self.timeout = timeout
        self.models.append(model)

    async def __aenter__(self) -> _FakeCopilotSessionManager:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, prompt: str) -> str:
        return "### Consensus Review Summary\n\n### Final Verdict\nApprove"


@pytest.mark.asyncio
async def test_generate_summary_uses_explicit_summary_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summary generation uses reviewer_summary_model, not reviewer_models[0]."""
    _FakeCopilotSessionManager.models = []
    monkeypatch.setattr(
        reviewer_module,
        "CopilotSessionManager",
        _FakeCopilotSessionManager,
    )
    config = AppConfig(
        copilot=CopilotConfig(
            reviewer_models=["claude-opus-4.6", "gpt-5.4"],
            reviewer_summary_model="gpt-5.4",
        ),
    )

    summary = await ReviewerAgent()._generate_summary(
        "Claude finding", "GPT finding", config,
    )

    assert _FakeCopilotSessionManager.models == ["gpt-5.4"]
    assert "AI Code Review Summary" in summary


@pytest.mark.asyncio
async def test_generate_suggestions_uses_explicit_suggestion_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suggestion generation uses reviewer_suggestion_model for formatting."""
    _FakeCopilotSessionManager.models = []
    monkeypatch.setattr(
        reviewer_module,
        "CopilotSessionManager",
        _FakeCopilotSessionManager,
    )
    monkeypatch.setattr(
        reviewer_module,
        "parse_suggestion_response",
        lambda raw: [],
    )
    config = AppConfig(
        copilot=CopilotConfig(
            reviewer_models=["claude-opus-4.6", "gpt-5.4"],
            reviewer_summary_model="gpt-5.4",
            reviewer_suggestion_model="claude-sonnet-4.6",
        ),
    )
    pr_context: dict[str, Any] = {
        "file_contents": {},
        "full_diff": "",
        "diff": "",
        "file_list": [],
        "changed_files": [],
    }

    await ReviewerAgent()._generate_and_post_suggestions(
        AsyncMock(),
        "acme",
        "webapp",
        7,
        "Claude finding",
        "GPT finding",
        "Consensus finding",
        pr_context,
        config,
    )

    assert _FakeCopilotSessionManager.models == ["claude-sonnet-4.6"]


@pytest.mark.asyncio
async def test_store_review_context_keeps_summary_hidden() -> None:
    """Stored review context should not render as a duplicate summary comment."""
    client = AsyncMock()
    client.get_issue_comments.return_value = []

    await ReviewerAgent()._store_review_context(
        client,
        "acme",
        "webapp",
        7,
        "## AI Code Review Summary\n\n### Consensus Review Summary\n\n| # | Title |",
        previous_context=None,
    )

    body = client.create_pr_comment.await_args.args[3]
    assert body.startswith("<!-- review-context-v1\n")
    assert body.endswith("\n/review-context -->")
    assert body.count("<!--") == 1
    assert "<!-- review-context-v1 -->\n## AI Code Review Summary" not in body


@pytest.mark.asyncio
async def test_load_review_context_accepts_hidden_and_legacy_markers() -> None:
    """Context loading remains compatible with old visible context comments."""
    client = AsyncMock()
    client.get_issue_comments.return_value = [
        {"body": "<!-- review-context-v1 -->\nold visible summary\n<!-- /review-context -->"},
        {"body": "<!-- review-context-v2\nnew hidden summary\n/review-context -->"},
    ]

    context = await ReviewerAgent()._load_review_context(
        client, "acme", "webapp", 7,
    )

    assert context == {"version": 2, "summary": "new hidden summary"}


@pytest.mark.asyncio
async def test_store_review_context_updates_legacy_comment_in_place() -> None:
    """A legacy rendered context comment is updated instead of duplicated."""
    client = AsyncMock()
    client.get_issue_comments.return_value = [
        {
            "id": 123,
            "body": "<!-- review-context-v1 -->\nold visible summary\n<!-- /review-context -->",
        }
    ]

    await ReviewerAgent()._store_review_context(
        client,
        "acme",
        "webapp",
        7,
        "## AI Code Review Summary\n\nUpdated summary",
        previous_context={"version": 1, "summary": "old visible summary"},
    )

    client.create_pr_comment.assert_not_called()
    client.update_issue_comment.assert_awaited_once()
    body = client.update_issue_comment.await_args.args[3]
    assert body.startswith("<!-- review-context-v2\n")
    assert body.endswith("\n/review-context -->")
