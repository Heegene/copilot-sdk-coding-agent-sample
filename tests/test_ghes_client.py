"""Tests for GHESClient API helper behavior."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

from agent.ghes_client import GHESClient


class TestGHESClient:
    async def test_update_issue_comment_uses_patch_endpoint(self) -> None:
        """update_issue_comment patches the issue comment resource."""
        client = GHESClient(host="github.example.com", token="token")
        request = AsyncMock(return_value={"id": 123, "body": "updated"})
        client._request = request  # type: ignore[method-assign]

        result = await client.update_issue_comment(
            "acme", "webapp", 123, "updated",
        )

        assert result == {"id": 123, "body": "updated"}
        request.assert_awaited_once_with(
            "PATCH",
            "/repos/acme/webapp/issues/comments/123",
            json={"body": "updated"},
        )

    async def test_remove_label_path_is_url_encoded(self) -> None:
        """Labels with separators or spaces are encoded in DELETE paths."""
        client = GHESClient(host="github.example.com", token="token")
        request = AsyncMock(return_value={"number": 7})
        client._request = request  # type: ignore[method-assign]

        await client.update_issue_labels(
            "acme", "webapp", 7, remove_labels=["bug/fix label"],
        )

        method, path = request.await_args_list[0].args[:2]
        assert method == "DELETE"
        assert path == "/repos/acme/webapp/issues/7/labels/bug%2Ffix%20label"

    async def test_content_path_is_url_encoded(self) -> None:
        """Repository content paths preserve / but encode path segment characters."""
        client = GHESClient(host="github.example.com", token="token")
        encoded = base64.b64encode(b"hello").decode()
        request = AsyncMock(return_value={"content": encoded})
        client._request = request  # type: ignore[method-assign]

        content = await client.get_file_content("acme", "webapp", "docs/My File.md")

        assert content == "hello"
        request.assert_awaited_once_with(
            "GET",
            "/repos/acme/webapp/contents/docs/My%20File.md",
            params={},
        )
