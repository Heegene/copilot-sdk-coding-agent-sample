"""Copilot SDK session management for the GHES Coding Agent.

Wraps the github-copilot-sdk Python package to provide a managed session
lifecycle with timeout handling, retry logic, multi-model support, and
a CLI fallback when the SDK is unavailable.

Authentication
--------------
The Copilot SDK/CLI and GHES API use **separate** credentials:

- ``GH_TOKEN``: A PAT from the GHES instance — used for issues, PRs, and
    git operations against GHES. It is not used for Copilot authentication.

- ``COPILOT_GITHUB_TOKEN`` (optional): An explicit GitHub token for Copilot
  SDK auth. This is the SDK's official env var and takes highest priority.

Deployment scenarios:
  - **Separate Copilot auth**: Set ``COPILOT_GITHUB_TOKEN`` with a
    GitHub.com fine-grained PAT that has Copilot access.
  - **Pre-authenticated runner**: Run ``copilot login`` once on the
        self-hosted runner. When no explicit Copilot token is set, ``GH_TOKEN``
        and ``GITHUB_TOKEN`` are stripped from Copilot subprocess environments so
        stored Copilot credentials can be used.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

import structlog

try:
    from copilot import CopilotClient as _CopilotClient

    try:
        from copilot import SubprocessConfig as _SubprocessConfig
    except ImportError:
        _SubprocessConfig = None

    CopilotClient: Any | None = _CopilotClient
    SubprocessConfig: Any | None = _SubprocessConfig
    SDK_AVAILABLE = True
except Exception as sdk_import_error:
    CopilotClient = None
    SubprocessConfig = None
    SDK_AVAILABLE = False
    SDK_IMPORT_ERROR: Exception | None = sdk_import_error
else:
    SDK_IMPORT_ERROR = None

logger = structlog.get_logger(__name__)

_UNSUPPORTED_COPILOT_ENV_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN")

DEFAULT_MODEL = "claude-sonnet-4.6"
MAX_CONCURRENT_SESSIONS = 5
_copilot_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
DEFAULT_TIMEOUT = 1800  # 30 minutes
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds


@contextmanager
def _without_unsupported_copilot_env_tokens() -> Iterator[None]:
    """Temporarily hide non-Copilot token env vars from SDK startup."""
    removed: dict[str, str] = {}
    for name in _UNSUPPORTED_COPILOT_ENV_TOKENS:
        value = os.environ.pop(name, None)
        if value is not None:
            removed[name] = value
    try:
        yield
    finally:
        os.environ.update(removed)


class CopilotSessionError(Exception):
    """Raised when a Copilot session encounters an unrecoverable error."""


class CopilotAuthenticationError(CopilotSessionError):
    """Raised when Copilot rejects the configured authentication."""


class CopilotTimeoutError(CopilotSessionError):
    """Raised when a Copilot session exceeds its timeout."""


class CopilotSDKUnavailableError(CopilotSessionError):
    """Raised when the Copilot SDK is not installed and no fallback works."""


@dataclass
class ToolDefinition:
    """A tool that can be registered with a Copilot session."""

    name: str
    description: str
    handler: Callable[..., Awaitable[Any]]


class CopilotSessionManager:
    """Manages the lifecycle of a Copilot SDK session.

    Supports the ``async with`` context-manager pattern, tool registration,
    multi-model parallel execution, and automatic CLI fallback when the SDK
    package is not installed.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        working_dir: str | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.working_dir = working_dir

        self._client: Any | None = None
        self._session: Any | None = None
        self._tools: dict[str, ToolDefinition] = {}
        self._started = False
        self._log = logger.bind(model=model)
        self._process: asyncio.subprocess.Process | None = None

    # -- context manager -----------------------------------------------------

    async def __aenter__(self) -> CopilotSessionManager:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the CopilotClient and create a session."""
        if self._started:
            self._log.warning("session_already_started")
            return

        if not SDK_AVAILABLE:
            self._log.info(
                "sdk_unavailable_using_cli_fallback",
                hint="pip install github-copilot-sdk",
                import_error=str(SDK_IMPORT_ERROR) if SDK_IMPORT_ERROR else None,
            )
            self._started = True
            return

        self._log.info("starting_copilot_session")

        copilot_token = os.environ.get("COPILOT_GITHUB_TOKEN")

        config_kwargs: dict[str, Any] = {}
        if self.working_dir:
            config_kwargs["cwd"] = self.working_dir
        if copilot_token:
            config_kwargs["github_token"] = copilot_token
            self._log.info("copilot_auth_explicit_token")
        else:
            self._log.info("copilot_auth_stored_credentials")

        auth_env = (
            nullcontext()
            if copilot_token
            else _without_unsupported_copilot_env_tokens()
        )
        try:
            with auth_env:
                self._client = self._create_sdk_client(config_kwargs)

                await self._client.__aenter__()

                session_kwargs: dict[str, Any] = {
                    "on_permission_request": self._handle_permission_request,
                    "model": self.model,
                }
                if copilot_token:
                    session_kwargs["github_token"] = copilot_token

                session = await self._create_sdk_session(session_kwargs)
                if hasattr(session, "__aenter__"):
                    self._session = await session.__aenter__()
                else:
                    self._session = session
        except Exception as exc:
            await self._cleanup_failed_start()
            raise self._normalize_start_error(exc, explicit_token=bool(copilot_token)) from exc

        self._started = True
        self._log.info("copilot_session_started")

    async def stop(self) -> None:
        """Tear down session and client, ignoring errors during cleanup."""
        if not self._started:
            return

        self._log.info("stopping_copilot_session")

        if self._session is not None:
            try:
                if hasattr(self._session, "__aexit__"):
                    await self._session.__aexit__(None, None, None)
                elif hasattr(self._session, "disconnect"):
                    await self._session.disconnect()
                elif hasattr(self._session, "destroy"):
                    await self._session.destroy()
            except Exception:
                self._log.warning("session_cleanup_error", exc_info=True)
            finally:
                self._session = None

        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                self._log.warning("client_cleanup_error", exc_info=True)
            finally:
                self._client = None

        self._started = False
        self._log.info("copilot_session_stopped")

    # -- execution ------------------------------------------------------------

    def _create_sdk_client(self, config_kwargs: dict[str, Any]) -> Any:
        """Create a Copilot SDK client across supported SDK constructor shapes."""
        if CopilotClient is None:
            raise CopilotSDKUnavailableError("Copilot SDK import failed")

        if not config_kwargs:
            return CopilotClient()

        if SubprocessConfig is not None:
            try:
                return CopilotClient(SubprocessConfig(**config_kwargs))
            except TypeError:
                pass

        keyword_kwargs: dict[str, Any] = {}
        if "cwd" in config_kwargs:
            keyword_kwargs["working_directory"] = config_kwargs["cwd"]
        if "github_token" in config_kwargs:
            keyword_kwargs["github_token"] = config_kwargs["github_token"]

        try:
            return CopilotClient(**keyword_kwargs)
        except TypeError:
            return CopilotClient(config_kwargs)

    async def _create_sdk_session(self, session_kwargs: dict[str, Any]) -> Any:
        """Create a Copilot SDK session across supported SDK method shapes."""
        if self._client is None:
            raise CopilotSessionError("Copilot SDK client was not started")

        try:
            return await self._client.create_session(**session_kwargs)
        except TypeError as exc:
            try:
                return await self._client.create_session(session_kwargs)
            except TypeError:
                raise exc

    async def _cleanup_failed_start(self) -> None:
        """Best-effort cleanup after SDK client startup or session creation fails."""
        if self._session is not None:
            try:
                if hasattr(self._session, "__aexit__"):
                    await self._session.__aexit__(None, None, None)
                elif hasattr(self._session, "disconnect"):
                    await self._session.disconnect()
                elif hasattr(self._session, "destroy"):
                    await self._session.destroy()
            except Exception:
                self._log.warning("session_cleanup_error", exc_info=True)
            finally:
                self._session = None

        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                self._log.warning("client_cleanup_error", exc_info=True)
            finally:
                self._client = None

    def _normalize_start_error(self, exc: Exception, *, explicit_token: bool) -> Exception:
        """Convert low-level SDK startup failures into actionable agent errors."""
        error_text = str(exc)
        if self._is_copilot_auth_error(error_text):
            if explicit_token:
                return CopilotAuthenticationError(
                    "Copilot authentication failed: COPILOT_GITHUB_TOKEN was provided "
                    "but GitHub Copilot rejected it. Use a GitHub.com fine-grained PAT "
                    "or OAuth token for a user with an active Copilot license and Copilot "
                    "access; do not use the GHES GH_TOKEN or a classic ghp_ PAT. To use "
                    "runner-stored auth instead, remove COPILOT_GITHUB_TOKEN and run "
                    "`copilot login` once on the self-hosted runner."
                )
            return CopilotAuthenticationError(
                "Copilot authentication failed: no valid stored Copilot credentials were "
                "available. Set COPILOT_GITHUB_TOKEN to a GitHub.com token for a Copilot "
                "licensed user, or run `copilot login` once on the self-hosted runner."
            )
        return exc

    def _is_copilot_auth_error(self, error_text: str) -> bool:
        """Return True when an SDK error indicates Copilot auth was rejected."""
        lowered = error_text.lower()
        auth_markers = (
            "failed to fetch copilot user info",
            "session was not created with authentication info",
            "401",
            "unauthorized",
            "authentication",
        )
        return any(marker in lowered for marker in auth_markers)

    async def execute(self, prompt: str) -> str:
        """Send *prompt* and return the full assistant response as a string.

        Retries up to ``MAX_RETRIES`` times on transient errors and enforces
        the configured timeout.
        """
        self._ensure_started()

        async with _copilot_semaphore:
            last_error: Exception | None = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    return await asyncio.wait_for(
                        self._execute_once(prompt),
                        timeout=self.timeout,
                    )
                except TimeoutError as exc:
                    await self._kill_subprocess()
                    raise CopilotTimeoutError(
                        f"Copilot session timed out after {self.timeout}s"
                    ) from exc
                except CopilotSessionError:
                    raise
                except Exception as exc:
                    last_error = exc
                    backoff = RETRY_BACKOFF_BASE**attempt
                    self._log.warning(
                        "transient_error_retrying",
                        attempt=attempt,
                        backoff=backoff,
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)

            raise CopilotSessionError(
                f"Failed after {MAX_RETRIES} retries: {last_error}"
            )

    async def execute_streaming(
        self,
        prompt: str,
        callback: Callable[[str], Any],
    ) -> str:
        """Send *prompt*, invoke *callback* for each token, return full response."""
        self._ensure_started()

        try:
            return await asyncio.wait_for(
                self._execute_streaming_once(prompt, callback),
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            raise CopilotTimeoutError(
                f"Copilot session timed out after {self.timeout}s"
            ) from exc

    # -- permission handling ----------------------------------------------------

    # Shell commands that could alter branch state or push code
    _BLOCKED_SHELL_PATTERNS: list[str] = [
        "git checkout", "git switch", "git branch",
        "git push", "git merge", "git rebase",
        "git reset", "git revert",
        "rm -rf", "rm -r /",
        "curl ", "wget ",
    ]

    def _handle_permission_request(self, request: Any, invocation: Any) -> Any:
        """Approve file read/write but block dangerous shell commands."""
        from copilot.session import PermissionRequestResult

        kind = getattr(request.kind, "value", str(request.kind))

        if kind == "shell":
            cmd = getattr(request, "full_command_text", "") or ""
            cmd_lower = cmd.lower().strip()
            for pattern in self._BLOCKED_SHELL_PATTERNS:
                if pattern in cmd_lower:
                    self._log.warning(
                        "permission_denied",
                        kind=kind,
                        command=cmd[:200],
                        blocked_by=pattern,
                    )
                    return PermissionRequestResult(
                        kind="denied-interactively-by-user",
                    )

        return PermissionRequestResult(kind="approved")

    # -- tool registration ----------------------------------------------------

    def register_tool(
        self,
        name: str,
        description: str,
        handler: Callable[..., Awaitable[Any]],
    ) -> None:
        """Register a tool the Copilot session may invoke."""
        self._tools[name] = ToolDefinition(
            name=name, description=description, handler=handler
        )
        self._log.info("tool_registered", tool=name)

    # -- multi-model ----------------------------------------------------------

    async def create_parallel_sessions(
        self, models: list[str]
    ) -> dict[str, CopilotSessionManager]:
        """Create independent ``CopilotSessionManager`` instances for each model."""
        sessions: dict[str, CopilotSessionManager] = {}
        for m in models:
            mgr = CopilotSessionManager(
                model=m, timeout=self.timeout, working_dir=self.working_dir
            )
            await mgr.start()
            sessions[m] = mgr
        return sessions

    async def execute_parallel(
        self, prompt: str, models: list[str]
    ) -> dict[str, str]:
        """Run *prompt* on multiple models concurrently, return ``{model: response}``."""
        sessions = await self.create_parallel_sessions(models)
        try:
            tasks = {
                m: asyncio.create_task(mgr.execute(prompt))
                for m, mgr in sessions.items()
            }
            results: dict[str, str] = {}
            for m, task in tasks.items():
                try:
                    results[m] = await task
                except Exception as exc:
                    self._log.error("parallel_execution_failed", model=m, error=str(exc))
                    results[m] = f"[error] {exc}"
            return results
        finally:
            for mgr in sessions.values():
                await mgr.stop()

    # -- internal helpers -----------------------------------------------------

    async def _kill_subprocess(self) -> None:
        """Kill the tracked subprocess if it is still running."""
        proc = self._process
        if proc is not None and proc.returncode is None:
            self._log.warning("killing_subprocess", pid=proc.pid)
            proc.kill()
            await proc.wait()
            self._process = None

    def _ensure_started(self) -> None:
        if not self._started:
            raise CopilotSessionError(
                "Session not started. Call start() or use 'async with'."
            )

    async def _execute_once(self, prompt: str) -> str:
        """Single execution attempt (SDK or CLI fallback)."""
        if not SDK_AVAILABLE or self._session is None:
            return await self._execute_via_cli(prompt)
        return await self._execute_via_sdk(prompt)

    async def _execute_streaming_once(
        self, prompt: str, callback: Callable[[str], Any]
    ) -> str:
        """Single streaming execution attempt (SDK or CLI fallback)."""
        if not SDK_AVAILABLE or self._session is None:
            result = await self._execute_via_cli(prompt)
            callback(result)
            return result
        return await self._execute_via_sdk(prompt, streaming_callback=callback)

    async def _execute_via_sdk(
        self,
        prompt: str,
        streaming_callback: Callable[[str], Any] | None = None,
    ) -> str:
        """Execute a prompt through the Copilot SDK session."""
        chunks: list[str] = []
        done = asyncio.Event()
        error_holder: list[str] = []

        def on_event(event: Any) -> None:
            etype = getattr(event.type, "value", str(event.type))
            if etype == "assistant.message":
                content = getattr(event.data, "content", "") or ""
                if not content:
                    return
                # Only keep the last message — earlier messages are
                # intermediate thinking steps emitted while tools run.
                chunks.clear()
                chunks.append(content)
                if streaming_callback is not None:
                    streaming_callback(content)
                self._log.debug("assistant_chunk", length=len(content))
            elif etype == "session.idle":
                done.set()
            elif etype == "session.error":
                msg = getattr(event.data, "message", str(event.data))
                error_holder.append(msg)
                self._log.error("session_error_event", error=msg)
                done.set()
            else:
                self._log.debug("session_event", event_type=etype)

        self._session.on(on_event)

        self._log.info("sending_prompt", length=len(prompt))
        await self._session.send(prompt)
        await done.wait()

        if error_holder:
            raise CopilotSessionError(
                f"Session error: {'; '.join(error_holder)}"
            )

        return "".join(chunks)

    async def _execute_via_cli(self, prompt: str) -> str:
        """Fallback: run the ``copilot`` CLI as a subprocess."""
        copilot_bin = shutil.which("copilot")
        if copilot_bin is None:
            raise CopilotSDKUnavailableError(
                "Neither the Copilot SDK nor the 'copilot' CLI is available. "
                "Install with: pip install github-copilot-sdk"
            )

        cmd = [
            copilot_bin,
            "-p",
            prompt,
            "--model",
            self.model,
            "--allow-all-tools",
        ]
        env = os.environ.copy()
        if "COPILOT_GITHUB_TOKEN" not in env:
            for name in _UNSUPPORTED_COPILOT_ENV_TOKENS:
                env.pop(name, None)

        self._log.info("executing_via_cli", model=self.model)
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
            env=env,
        )
        proc = self._process
        stdout, stderr = await proc.communicate()
        self._process = None

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            self._log.error("cli_execution_failed", returncode=proc.returncode, stderr=err_msg)
            raise CopilotSessionError(
                f"Copilot CLI exited with code {proc.returncode}: {err_msg}"
            )

        return stdout.decode(errors="replace").strip()


# -- convenience function ----------------------------------------------------


async def run_copilot(
    prompt: str,
    model: str = DEFAULT_MODEL,
    working_dir: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """One-shot convenience function to run a prompt and get a response."""
    async with CopilotSessionManager(
        model=model, timeout=timeout, working_dir=working_dir
    ) as mgr:
        return await mgr.execute(prompt)
