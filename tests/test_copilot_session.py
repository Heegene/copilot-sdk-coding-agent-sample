"""Tests for Copilot session authentication environment handling."""

from __future__ import annotations

import os

from pytest import MonkeyPatch

import agent.copilot_session as module
from agent.copilot_session import (
    CopilotSessionManager,
    _without_unsupported_copilot_env_tokens,
)


class FakeProcess:
    """Minimal subprocess result for CLI fallback tests."""

    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        """Return successful stdout/stderr bytes."""
        return b"ok", b""


def test_without_unsupported_copilot_env_tokens_restores_values(
    monkeypatch: MonkeyPatch,
) -> None:
    """GHES and Actions tokens are hidden only inside the auth guard."""
    monkeypatch.setenv("GH_TOKEN", "ghes-token")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-token")

    with _without_unsupported_copilot_env_tokens():
        assert "GH_TOKEN" not in os.environ
        assert "GITHUB_TOKEN" not in os.environ
        assert os.environ["COPILOT_GITHUB_TOKEN"] == "copilot-token"

    assert os.environ["GH_TOKEN"] == "ghes-token"
    assert os.environ["GITHUB_TOKEN"] == "actions-token"
    assert os.environ["COPILOT_GITHUB_TOKEN"] == "copilot-token"


async def test_cli_fallback_strips_non_copilot_tokens(
    monkeypatch: MonkeyPatch,
) -> None:
    """CLI fallback does not pass GHES or Actions tokens as Copilot credentials."""
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(
        *command: str,
        **kwargs: object,
    ) -> FakeProcess:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/copilot")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setenv("GH_TOKEN", "ghes-token")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)

    result = await CopilotSessionManager()._execute_via_cli("hello")

    env = captured["env"]
    assert result == "ok"
    assert isinstance(env, dict)
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
