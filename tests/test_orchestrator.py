"""Tests for orchestrator routing logic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agent.config import AppConfig
from agent.orchestrator import Orchestrator
from agent.triggers.label_trigger import AgentType, TriggerContext


def _make_ctx(agent_type: AgentType, **overrides) -> TriggerContext:
    defaults = dict(
        agent_type=agent_type,
        event_type="issues",
        owner="acme",
        repo="webapp",
        issue_number=1,
        pr_number=None,
        issue_title="Test",
        issue_body="body",
        creator="user",
        server_url="https://github.example.com",
        run_id=None,
    )
    defaults.update(overrides)
    return TriggerContext(**defaults)


class TestOrchestratorRouting:
    def setup_method(self):
        self.config = AppConfig()
        self.orchestrator = Orchestrator(self.config)

    async def test_route_issue_labeled_copilot(self, tmp_path):
        """Issues labeled 'copilot' route to coder agent."""
        event = {
            "action": "labeled",
            "label": {"name": "copilot"},
            "issue": {
                "number": 1,
                "title": "Fix bug",
                "body": "Something is broken",
                "user": {"login": "dev"},
                "html_url": "https://ghes.example.com/org/repo/issues/1",
                "repository_url": "https://ghes.example.com/repos/org/repo",
            },
        }
        event_file = tmp_path / "event.json"
        event_file.write_text(json.dumps(event))

        with patch.dict("os.environ", {
            "GITHUB_EVENT_PATH": str(event_file),
            "GITHUB_EVENT_NAME": "issues",
        }), patch.object(
            self.orchestrator, "_route_agent", new_callable=AsyncMock, return_value="done"
        ) as mock_route, patch(
            "agent.orchestrator.load_repo_config",
            new_callable=AsyncMock,
            return_value=self.config,
        ), patch.object(
            self.orchestrator, "_post_progress", new_callable=AsyncMock
        ):
            self.orchestrator.ghes_client = MagicMock()
            self.orchestrator.ghes_client.close = AsyncMock()
            await self.orchestrator.run()
            mock_route.assert_called_once()
            ctx = mock_route.call_args[0][0]
            assert ctx.agent_type == AgentType.CODER

    async def test_route_pr_labeled_copilot(self, tmp_path):
        """PR labeled 'copilot-review' routes to reviewer agent."""
        event = {
            "action": "labeled",
            "label": {"name": "copilot-review"},
            "pull_request": {
                "number": 5,
                "title": "feat: stuff",
                "body": "PR body",
                "user": {"login": "dev"},
                "html_url": "https://ghes.example.com/org/repo/pull/5",
                "repository_url": "https://ghes.example.com/repos/org/repo",
            },
        }
        event_file = tmp_path / "event.json"
        event_file.write_text(json.dumps(event))

        with patch.dict("os.environ", {
            "GITHUB_EVENT_PATH": str(event_file),
            "GITHUB_EVENT_NAME": "pull_request",
        }), patch.object(
            self.orchestrator, "_route_agent", new_callable=AsyncMock, return_value="done"
        ) as mock_route, patch(
            "agent.orchestrator.load_repo_config",
            new_callable=AsyncMock,
            return_value=self.config,
        ), patch.object(
            self.orchestrator, "_post_progress", new_callable=AsyncMock
        ):
            self.orchestrator.ghes_client = MagicMock()
            self.orchestrator.ghes_client.close = AsyncMock()
            await self.orchestrator.run()
            mock_route.assert_called_once()
            ctx = mock_route.call_args[0][0]
            assert ctx.agent_type == AgentType.REVIEWER

    async def test_route_unknown_event(self, tmp_path):
        """Unknown events are handled gracefully (no crash)."""
        event = {"action": "opened", "issue": {"number": 1}}
        event_file = tmp_path / "event.json"
        event_file.write_text(json.dumps(event))

        with patch.dict("os.environ", {
            "GITHUB_EVENT_PATH": str(event_file),
            "GITHUB_EVENT_NAME": "unknown_event",
        }), patch.object(
            self.orchestrator, "_route_agent", new_callable=AsyncMock
        ) as mock_route:
            await self.orchestrator.run()
            mock_route.assert_not_called()

    async def test_loads_repo_config_before_routing(self, tmp_path: Path) -> None:
        """Per-repository config is loaded after trigger parsing and before routing."""
        event = {
            "action": "labeled",
            "label": {"name": "copilot"},
            "issue": {
                "number": 1,
                "title": "Fix bug",
                "body": "Something is broken",
                "user": {"login": "dev"},
                "html_url": "https://ghes.example.com/org/repo/issues/1",
                "repository_url": "https://ghes.example.com/repos/org/repo",
            },
        }
        event_file = tmp_path / "event.json"
        event_file.write_text(json.dumps(event))
        merged_config = self.config.model_copy(deep=True)
        merged_config.agent.output_language = "ko"

        with patch.dict("os.environ", {
            "GITHUB_EVENT_PATH": str(event_file),
            "GITHUB_EVENT_NAME": "issues",
        }), patch.object(
            self.orchestrator, "_route_agent", new_callable=AsyncMock, return_value="done"
        ) as mock_route, patch.object(
            self.orchestrator, "_post_progress", new_callable=AsyncMock
        ), patch(
            "agent.orchestrator.load_repo_config",
            new_callable=AsyncMock,
            return_value=merged_config,
        ) as mock_load_repo_config:
            await self.orchestrator.run()

        mock_load_repo_config.assert_awaited_once()
        assert mock_load_repo_config.await_args.args[1] == "org/repo"
        assert self.orchestrator.config.agent.output_language == "ko"
        mock_route.assert_called_once()
