"""Tests for CoderAgent PR body behavior."""

from __future__ import annotations

import pytest

import agent.agents.coder_agent as coder_module
from agent.agents.coder_agent import CoderAgent
from agent.config import AppConfig, CopilotConfig
from agent.triggers.label_trigger import AgentType, TriggerContext


class _FakeCopilotSessionManager:
    """Minimal async context manager that captures the PR summary model."""

    models: list[str] = []

    def __init__(self, *, model: str, timeout: int, working_dir: str | None = None) -> None:
        self.model = model
        self.timeout = timeout
        self.working_dir = working_dir
        self.models.append(model)

    async def __aenter__(self) -> _FakeCopilotSessionManager:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, prompt: str) -> str:
        return (
            "### SUMMARY\n"
            "- Built a movie review experience with routing and shared state."
        )


class _FailingCopilotSessionManager:
    """Async context manager that simulates PR summary model failure."""

    def __init__(self, *, model: str, timeout: int, working_dir: str | None = None) -> None:
        self.model = model
        self.timeout = timeout
        self.working_dir = working_dir

    async def __aenter__(self) -> _FailingCopilotSessionManager:
        raise RuntimeError("model unavailable")

    async def __aexit__(self, *exc: object) -> None:
        return None


def _make_issue_ctx() -> TriggerContext:
    """Build a minimal CoderAgent issue context."""
    return TriggerContext(
        agent_type=AgentType.CODER,
        event_type="issues",
        owner="acme",
        repo="movies",
        issue_number=42,
        pr_number=None,
        issue_title="Build movie review app",
        issue_body="Create a latest review feed with login and signup routes.",
        creator="dev",
        server_url="https://github.example.com",
        run_id=None,
    )


@pytest.mark.asyncio
async def test_pr_body_uses_lightweight_summary_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR body generation uses coder_pr_summary_model and omits full file lists."""

    async def fake_run_git(*args: str, cwd: str) -> str:
        if args[:2] == ("diff", "--name-status"):
            return "A\tsrc/App.jsx\nA\tpackage.json\nM\tREADME.md\n"
        if args[:2] == ("diff", "--stat"):
            return " 3 files changed, 120 insertions(+), 4 deletions(-)"
        if args[:2] == ("diff", "--shortstat"):
            return "3 files changed, 120 insertions(+), 4 deletions(-)"
        if args[:2] == ("log", "origin/main..HEAD"):
            return "feat: build movie review app\n"
        return ""

    _FakeCopilotSessionManager.models = []
    monkeypatch.setattr(coder_module, "_run_git", fake_run_git)
    monkeypatch.setattr(
        coder_module,
        "CopilotSessionManager",
        _FakeCopilotSessionManager,
    )
    config = AppConfig(
        copilot=CopilotConfig(coder_pr_summary_model="gpt-5.4-mini"),
    )

    body = await CoderAgent()._build_pr_body(
        _make_issue_ctx(), ".", "main", "en", config,
    )

    assert _FakeCopilotSessionManager.models == ["gpt-5.4-mini"]
    assert "Built a movie review experience" in body
    assert "## Verification" not in body
    assert "## Change Footprint" in body
    assert "Changed 3 file(s)." in body
    assert "Files changed" in body
    assert "`src/App.jsx`" not in body
    assert "## Changes" not in body


@pytest.mark.asyncio
async def test_pr_body_falls_back_when_summary_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR creation remains deterministic if the lightweight summary call fails."""

    async def fake_run_git(*args: str, cwd: str) -> str:
        if args[:2] == ("diff", "--name-status"):
            return "M\tsrc/App.jsx\n"
        if args[:2] == ("diff", "--shortstat"):
            return "1 file changed, 10 insertions(+)"
        return ""

    monkeypatch.setattr(coder_module, "_run_git", fake_run_git)
    monkeypatch.setattr(
        coder_module,
        "CopilotSessionManager",
        _FailingCopilotSessionManager,
    )
    config = AppConfig(
        copilot=CopilotConfig(coder_pr_summary_model="gpt-5.4-mini"),
    )

    body = await CoderAgent()._build_pr_body(
        _make_issue_ctx(), ".", "main", "en", config,
    )

    assert "This PR addresses **Build movie review app**." in body
    assert "No verification command was captured by the agent" not in body
    assert "## Verification" not in body
    assert "Changed 1 file(s)." in body
    assert "## Changes" not in body
