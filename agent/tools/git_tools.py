"""Git operations via async subprocess.

All functions shell out to ``git`` using :func:`asyncio.create_subprocess_exec`
so they never block the event loop.  A custom *cwd* can be passed to every
helper; when omitted the current working directory is used.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


async def _run_git(
    *args: str,
    cwd: str | Path | None = None,
) -> str:
    """Run a git command and return stdout.

    Raises :class:`RuntimeError` on non-zero exit.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    stdout_str = stdout.decode().strip()
    stderr_str = stderr.decode().strip()

    if proc.returncode != 0:
        logger.error(
            "git_command_failed",
            args=args,
            returncode=proc.returncode,
            stderr=stderr_str,
        )
        msg = f"git {args[0]} failed (rc={proc.returncode}): {stderr_str}"
        raise RuntimeError(msg)

    logger.debug("git_command_ok", args=args, stdout_preview=stdout_str[:200])
    return stdout_str


# ------------------------------------------------------------------
# Public helpers
# ------------------------------------------------------------------


async def git_checkout_new_branch(
    branch_name: str,
    base: str,
    *,
    cwd: str | Path | None = None,
) -> str:
    """Create and switch to a new branch from *base*."""
    await _run_git("fetch", "origin", base, cwd=cwd)
    return await _run_git("checkout", "-b", branch_name, f"origin/{base}", cwd=cwd)


async def git_checkout_existing_branch(
    branch_name: str,
    *,
    cwd: str | Path | None = None,
) -> str:
    """Fetch and checkout an existing remote branch."""
    await _run_git("fetch", "origin", branch_name, cwd=cwd)
    return await _run_git("checkout", branch_name, cwd=cwd)


async def git_branch_exists_remote(
    branch_name: str,
    *,
    cwd: str | Path | None = None,
) -> bool:
    """Check if a branch exists on the remote."""
    try:
        await _run_git("ls-remote", "--exit-code", "--heads", "origin", branch_name, cwd=cwd)
        return True
    except RuntimeError:
        return False


async def git_add_all(*, cwd: str | Path | None = None) -> str:
    """Stage all changes (``git add -A``)."""
    return await _run_git("add", "-A", cwd=cwd)


async def git_commit(
    message: str,
    co_author: str | None = None,
    *,
    cwd: str | Path | None = None,
) -> str:
    """Create a commit.  Appends a ``Co-authored-by`` trailer when provided."""
    full_message = message
    if co_author:
        full_message = f"{message}\n\nCo-authored-by: {co_author}"
    return await _run_git("commit", "-m", full_message, cwd=cwd)


async def git_push(
    branch: str,
    force: bool = False,
    *,
    cwd: str | Path | None = None,
) -> str:
    """Push *branch* to ``origin``."""
    args = ["push", "origin", branch]
    if force:
        args.insert(1, "--force")
    return await _run_git(*args, cwd=cwd)


async def git_diff(
    staged: bool = False,
    *,
    cwd: str | Path | None = None,
) -> str:
    """Return the current diff (or staged diff)."""
    args = ["diff"]
    if staged:
        args.append("--cached")
    return await _run_git(*args, cwd=cwd)


async def git_status(*, cwd: str | Path | None = None) -> str:
    """Return ``git status --short``."""
    return await _run_git("status", "--short", cwd=cwd)


async def git_rev_parse(ref: str = "HEAD", *, cwd: str | Path | None = None) -> str:
    """Return the full SHA for *ref*."""
    return await _run_git("rev-parse", ref, cwd=cwd)


async def git_log(n: int = 10, *, cwd: str | Path | None = None) -> str:
    """Return the last *n* log entries (one-line format)."""
    return await _run_git("log", "--oneline", f"-{n}", cwd=cwd)


async def configure_git_user(
    name: str = "github-actions[bot]",
    email: str = "github-actions[bot]@users.noreply.github.com",
    *,
    cwd: str | Path | None = None,
) -> None:
    """Configure ``user.name`` and ``user.email`` locally."""
    await _run_git("config", "user.name", name, cwd=cwd)
    await _run_git("config", "user.email", email, cwd=cwd)
    logger.info("git_user_configured", name=name, email=email)


async def configure_git_credentials(
    host: str,
    token: str,
    *,
    cwd: str | Path | None = None,
) -> None:
    """Store credentials for *host* using the git-credential ``store`` helper.

    Writes an entry to the credential store so that subsequent ``git push``
    commands authenticate automatically against a GHES instance.
    """
    await _run_git("config", "credential.helper", "store", cwd=cwd)

    proc = await asyncio.create_subprocess_exec(
        "git",
        "credential",
        "approve",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    credential_input = (
        f"protocol=https\nhost={host}\nusername=x-access-token\npassword={token}\n\n"
    )
    stdout, stderr = await proc.communicate(credential_input.encode())
    if proc.returncode != 0:
        raise RuntimeError(f"git credential approve failed: {stderr.decode().strip()}")

    logger.info("git_credentials_configured", host=host)
