"""Agent workflow tests for no-change Copilot outputs."""

from __future__ import annotations

from unittest.mock import AsyncMock

from pytest import MonkeyPatch

from agent.agents.ci_fix_agent import CIFixAgent
from agent.agents.doc_gen_agent import DocGenAgent
from agent.config import AppConfig
from agent.triggers.label_trigger import AgentType, TriggerContext


class FakeCopilotSession:
    """Minimal async context manager used to avoid live Copilot calls."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> FakeCopilotSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, prompt: str) -> str:
        return "analysed but did not edit files"


def _make_ctx(agent_type: AgentType, **overrides: object) -> TriggerContext:
    defaults = dict(
        agent_type=agent_type,
        event_type="issues",
        owner="acme",
        repo="webapp",
        issue_number=1,
        pr_number=None,
        issue_title="Test",
        issue_body="body",
        creator="dev",
        server_url="https://github.example.com",
        run_id=None,
    )
    defaults.update(overrides)
    return TriggerContext(**defaults)


class TestAgentNoChanges:
    async def test_doc_gen_skips_commit_when_no_files_changed(
        self, monkeypatch: MonkeyPatch,
    ) -> None:
        """DocGenAgent reports no changes instead of committing an empty tree."""
        import agent.agents.doc_gen_agent as module

        monkeypatch.setattr(module, "CopilotSessionManager", FakeCopilotSession)
        git_commit = AsyncMock()
        git_push = AsyncMock()
        monkeypatch.setattr(module, "git_branch_exists_remote", AsyncMock(return_value=False))
        monkeypatch.setattr(module, "git_checkout_new_branch", AsyncMock())
        monkeypatch.setattr(module, "git_checkout_existing_branch", AsyncMock())
        monkeypatch.setattr(module, "git_rev_parse", AsyncMock(side_effect=["abc", "abc"]))
        monkeypatch.setattr(module, "git_add_all", AsyncMock())
        monkeypatch.setattr(module, "git_diff", AsyncMock(return_value=""))
        monkeypatch.setattr(module, "git_commit", git_commit)
        monkeypatch.setattr(module, "git_push", git_push)

        ghes_client = AsyncMock()
        ghes_client.get_file_content.side_effect = Exception("missing")
        ghes_client.create_issue_comment = AsyncMock(return_value={"id": 1})

        result = await DocGenAgent().execute(
            _make_ctx(AgentType.DOC_GEN), ghes_client, AppConfig(),
        )

        assert "no file changes" in result
        git_commit.assert_not_awaited()
        git_push.assert_not_awaited()

    async def test_ci_fix_skips_commit_when_no_files_changed(
        self, monkeypatch: MonkeyPatch,
    ) -> None:
        """CIFixAgent reports no changes instead of committing an empty tree."""
        import agent.agents.ci_fix_agent as module

        monkeypatch.setattr(module, "CopilotSessionManager", FakeCopilotSession)
        git_commit = AsyncMock()
        git_push = AsyncMock()
        monkeypatch.setattr(module, "git_checkout_existing_branch", AsyncMock())
        monkeypatch.setattr(module, "git_rev_parse", AsyncMock(side_effect=["abc", "abc"]))
        monkeypatch.setattr(module, "git_add_all", AsyncMock())
        monkeypatch.setattr(module, "git_diff", AsyncMock(return_value=""))
        monkeypatch.setattr(module, "git_commit", git_commit)
        monkeypatch.setattr(module, "git_push", git_push)

        ghes_client = AsyncMock()
        ghes_client.get_pull_request.return_value = {"head": {"ref": "copilot/1", "sha": "abc"}}
        ghes_client.get_workflow_run_logs.return_value = b"pytest failed in tests/test_app.py"
        ghes_client.list_workflow_run_jobs.return_value = [
            {"name": "tests", "conclusion": "failure"},
        ]
        ghes_client.get_file_content.side_effect = Exception("missing")
        ghes_client.create_pr_comment = AsyncMock(return_value={"id": 1})

        result = await CIFixAgent().execute(
            _make_ctx(AgentType.CI_FIX, pr_number=1, issue_number=None, run_id="123"),
            ghes_client,
            AppConfig(),
        )

        assert "generated no file changes" in result
        git_commit.assert_not_awaited()
        git_push.assert_not_awaited()
