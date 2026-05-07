"""GHES REST API client using httpx.AsyncClient.

Supports both github.com and GitHub Enterprise Server instances.
Authenticates via Bearer token using ``GH_TOKEN`` for GHES API operations.
Copilot authentication is separate and uses ``COPILOT_GITHUB_TOKEN`` or
credentials stored by ``copilot login`` on the runner.
"""

from __future__ import annotations

import base64
import random
import time
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger(__name__)


def _quote_path(path: str) -> str:
    """Quote a repository content path while preserving path separators."""
    return quote(path, safe="/")

# ---------------------------------------------------------------------------
# Retry predicate: retry on 429 (rate-limit) and 5xx (server errors)
# ---------------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return (
            exc.response.status_code == 429
            or exc.response.status_code >= 500
        )
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout))


def _wait_for_rate_limit(retry_state: Any) -> float:
    """Rate limit 헤더를 파싱하여 적절한 대기 시간을 결정한다."""
    exc = retry_state.outcome.exception()
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 429
    ):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            return float(retry_after) + random.uniform(0, 2)
        reset_at = exc.response.headers.get("X-RateLimit-Reset")
        if reset_at:
            wait_seconds = max(0, float(reset_at) - time.time()) + 1
            return min(wait_seconds, 300) + random.uniform(0, 2)
    return random.uniform(0, 2)


_retry_policy = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30)
    + _wait_for_rate_limit,
    reraise=True,
)


class GHESClient:
    """Async GitHub / GHES REST API client.

    Uses ``GH_TOKEN`` for GitHub/GHES API calls. Copilot authentication is
    configured separately by ``COPILOT_GITHUB_TOKEN`` or stored Copilot login
    credentials.

    Args:
        host: GitHub hostname (e.g. ``"github.com"`` or ``"ghes.example.com"``).
        token: Personal Access Token (``GH_TOKEN``).
        api_base: Full API base URL.  When *None* it is derived from *host*:
            - ``github.com`` → ``https://api.github.com``
            - anything else  → ``https://{host}/api/v3``
    """

    def __init__(
        self,
        host: str,
        token: str,
        api_base: str | None = None,
    ) -> None:
        self.host = host
        self.token = token

        if api_base is not None:
            self.api_base = api_base.rstrip("/")
        elif host.lower() in ("github.com", "www.github.com"):
            self.api_base = "https://api.github.com"
        else:
            self.api_base = f"https://{host}/api/v3"

        self._client: httpx.AsyncClient | None = None
        self._log = logger.bind(host=self.host)

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> GHESClient:
        self._client = httpx.AsyncClient(
            base_url=self.api_base,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
            ),
        )
        self._log.info("ghes_client.opened", api_base=self.api_base)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Explicitly close the underlying HTTP client.

        Safe to call multiple times or before the client has been opened.
        """
        if self._client:
            await self._client.aclose()
            self._client = None
            self._log.info("ghes_client.closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            msg = "GHESClient must be used as an async context manager"
            raise RuntimeError(msg)
        return self._client

    @_retry_policy
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send an HTTP request and return the parsed JSON response.

        Raises ``httpx.HTTPStatusError`` for non-2xx responses after retries
        are exhausted.
        """
        self._log.debug("ghes_client.request", method=method, path=path)
        resp = await self.client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            body_preview = resp.text[:500] if resp.text else "(empty)"
            self._log.error(
                "ghes_client.http_error",
                method=method,
                path=path,
                status=resp.status_code,
                body=body_preview,
            )
        resp.raise_for_status()

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def _request_raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send a request and return the raw ``httpx.Response``."""
        self._log.debug("ghes_client.request_raw", method=method, path=path)
        resp = await self.client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp

    # ==================================================================
    # Issues
    # ==================================================================

    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """Get issue details.

        Returns the full issue object as described in the GitHub REST API docs.
        """
        return await self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")

    async def update_issue_labels(
        self,
        owner: str,
        repo: str,
        number: int,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add and/or remove labels on an issue.

        Returns the updated issue object.
        """
        if add_labels:
            await self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{number}/labels",
                json={"labels": add_labels},
            )

        for label in remove_labels or []:
            encoded_label = quote(label, safe="")
            try:
                await self._request(
                    "DELETE",
                    f"/repos/{owner}/{repo}/issues/{number}/labels/{encoded_label}",
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                self._log.warning("label_not_found", label=label, issue=number)

        return await self.get_issue(owner, repo, number)

    async def create_issue_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        """Post a comment on an issue. Returns the created comment object."""
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )

    async def update_issue_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        body: str,
    ) -> dict[str, Any]:
        """Update an existing issue or pull-request comment."""
        return await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )

    async def get_issue_comments(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        """이슈의 모든 코멘트를 페이지네이션하여 가져온다."""
        all_items: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            items: list[dict[str, Any]] = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                params={"page": page, "per_page": per_page},
            )
            all_items.extend(items)
            if len(items) < per_page:
                break
            page += 1
        return all_items

    async def get_pr_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """PR의 모든 리뷰 코멘트를 페이지네이션하여 가져온다."""
        all_items: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            items: list[dict[str, Any]] = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
                params={"page": page, "per_page": per_page},
            )
            all_items.extend(items)
            if len(items) < per_page:
                break
            page += 1
        return all_items

    async def find_pr_by_branch(
        self, owner: str, repo: str, branch: str, *, state: str = "all",
    ) -> dict[str, Any] | None:
        """Find a PR with the given head branch. Returns the most recent match or None.

        Args:
            state: PR state filter — ``"open"``, ``"closed"``, or ``"all"`` (default).
        """
        prs = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"head": f"{owner}:{branch}", "state": state},
        )
        return prs[0] if prs else None

    # ==================================================================
    # Pull Requests
    # ==================================================================

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        assignees: list[str] | None = None,
        draft: bool = False,
    ) -> dict[str, Any]:
        """Create a pull request. Returns the created PR object."""
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        }
        pr: dict[str, Any] = await self._request(
            "POST", f"/repos/{owner}/{repo}/pulls", json=payload
        )

        if assignees:
            await self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{pr['number']}/assignees",
                json={"assignees": assignees},
            )
            pr = await self.get_pull_request(owner, repo, pr["number"])

        return pr

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """Get pull request details."""
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    async def get_pull_request_diff(self, owner: str, repo: str, number: int) -> str:
        """Get the diff for a pull request as a plain-text string."""
        resp = await self._request_raw(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    async def get_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        """PR에서 변경된 모든 파일을 페이지네이션하여 가져온다."""
        all_items: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            items: list[dict[str, Any]] = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"page": page, "per_page": per_page},
            )
            all_items.extend(items)
            if len(items) < per_page:
                break
            page += 1
        return all_items

    async def create_pr_review(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        event: str = "COMMENT",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit a pull request review.

        Args:
            event: One of ``COMMENT``, ``APPROVE``, or ``REQUEST_CHANGES``.
            comments: Optional list of inline review comment dicts. Each dict
                should have ``path``, ``line``, ``side``, and ``body`` keys.
        """
        payload: dict[str, Any] = {"body": body, "event": event}
        if comments:
            payload["comments"] = comments
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json=payload,
        )

    async def create_pr_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        """Post an issue-style comment on a pull request."""
        return await self.create_issue_comment(owner, repo, number, body)

    # ==================================================================
    # Repository contents
    # ==================================================================

    async def get_file_content(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str:
        """Get decoded file content (UTF-8) from the repository.

        Args:
            ref: Branch, tag, or commit SHA. Uses the default branch when *None*.

        Returns:
            The decoded file content as a string.
        """
        params: dict[str, str] = {}
        if ref:
            params["ref"] = ref

        encoded_path = _quote_path(path)
        data: dict[str, Any] = await self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{encoded_path}", params=params
        )
        encoded: str = data.get("content", "")
        return base64.b64decode(encoded).decode()

    async def get_directory_contents(
        self, owner: str, repo: str, path: str = "", ref: str | None = None
    ) -> list[dict[str, Any]]:
        """List directory entries in a repository path.

        Returns a list of content objects (name, path, type, size, …).
        """
        params: dict[str, str] = {}
        if ref:
            params["ref"] = ref

        encoded_path = _quote_path(path)
        return await self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{encoded_path}", params=params
        )

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str | None = None,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file in the repository.

        Args:
            content: Raw text content (will be base64-encoded automatically).
            message: Commit message.
            branch: Target branch. Uses the default branch when *None*.
            sha: Blob SHA of the file being replaced (required for updates).

        Returns:
            The ``content`` response object from the GitHub API.
        """
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
        }
        if branch:
            payload["branch"] = branch
        if sha:
            payload["sha"] = sha

        encoded_path = _quote_path(path)
        return await self._request(
            "PUT", f"/repos/{owner}/{repo}/contents/{encoded_path}", json=payload
        )

    # ==================================================================
    # Workflow Runs
    # ==================================================================

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        """Get details of a workflow run."""
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")

    async def get_workflow_run_logs(self, owner: str, repo: str, run_id: int) -> bytes:
        """Download workflow run logs as a zip archive (raw bytes).

        The GitHub API redirects to a short-lived URL that serves the zip.
        """
        resp = await self._request_raw(
            "GET",
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            follow_redirects=True,
        )
        return resp.content

    async def list_workflow_run_jobs(
        self, owner: str, repo: str, run_id: int
    ) -> list[dict[str, Any]]:
        """워크플로우 런의 모든 job을 페이지네이션하여 가져온다."""
        all_jobs: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            data: dict[str, Any] = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                params={"page": page, "per_page": per_page},
            )
            jobs = data.get("jobs", [])
            all_jobs.extend(jobs)
            if len(jobs) < per_page:
                break
            page += 1
        return all_jobs
