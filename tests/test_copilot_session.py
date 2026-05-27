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


class FakeSubprocessConfig:
    """Capture SDK subprocess config values."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakeSessionContext:
    """Async context manager returned by create_session."""

    async def __aenter__(self) -> object:
        """Return a placeholder session object."""
        return object()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Clean up the fake session."""


class FakeCopilotClient:
    """Capture SDK client and session construction arguments."""

    instances: list[FakeCopilotClient] = []

    def __init__(self, config: FakeSubprocessConfig | None = None) -> None:
        self.config = config
        self.create_session_kwargs: dict[str, object] | None = None
        FakeCopilotClient.instances.append(self)

    async def __aenter__(self) -> FakeCopilotClient:
        """Start the fake client."""
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Stop the fake client."""

    async def create_session(self, **kwargs: object) -> FakeSessionContext:
        """Capture session creation kwargs."""
        self.create_session_kwargs = kwargs
        return FakeSessionContext()


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


async def test_sdk_session_receives_explicit_copilot_token(
    monkeypatch: MonkeyPatch,
) -> None:
    """Explicit Copilot auth is passed to both client and session creation."""
    FakeCopilotClient.instances.clear()
    monkeypatch.setattr(module, "SDK_AVAILABLE", True)
    monkeypatch.setattr(module, "CopilotClient", FakeCopilotClient)
    monkeypatch.setattr(module, "SubprocessConfig", FakeSubprocessConfig)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_copilot")

    manager = CopilotSessionManager(working_dir="/workspace")
    await manager.start()

    client = FakeCopilotClient.instances[0]
    assert client.config is not None
    assert client.config.kwargs["cwd"] == "/workspace"
    assert client.config.kwargs["github_token"] == "github_pat_copilot"
    assert client.create_session_kwargs is not None
    assert client.create_session_kwargs["github_token"] == "github_pat_copilot"

    await manager.stop()
