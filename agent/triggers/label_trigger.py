"""Label-based trigger handler for GitHub Actions events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import structlog

log = structlog.get_logger(__name__)


class AgentType(Enum):
    CODER = "coder"
    REVIEWER = "reviewer"
    DOC_GEN = "doc_gen"
    CI_FIX = "ci_fix"


@dataclass
class TriggerContext:
    agent_type: AgentType
    event_type: str  # "issues", "pull_request", "workflow_run"
    owner: str
    repo: str
    issue_number: int | None
    pr_number: int | None
    issue_title: str
    issue_body: str
    creator: str
    server_url: str
    run_id: str | None  # for workflow_run events
    trigger_label: str = ""  # the label that triggered this agent


class LabelTrigger:
    """Handles label-based triggers from GitHub issues and pull requests."""

    LABEL_MAP: dict[str, AgentType] = {
        "copilot": AgentType.CODER,
        "copilot-review": AgentType.REVIEWER,
        "copilot-docs": AgentType.DOC_GEN,
        "copilot-fix": AgentType.CI_FIX,
    }

    def parse(self, event_payload: dict) -> TriggerContext | None:
        """Parse a GitHub Actions event payload for label-based triggers.

        Supports issues.labeled and pull_request.labeled events.
        Returns None if the label doesn't match any known trigger.
        """
        action = event_payload.get("action")
        if action != "labeled":
            log.debug("ignoring non-labeled action", action=action)
            return None

        label_name = event_payload.get("label", {}).get("name", "")
        agent_type = self.LABEL_MAP.get(label_name)
        if agent_type is None:
            log.debug("label not in trigger map", label=label_name)
            return None

        # Determine event type: PR or issue
        pr_data = event_payload.get("pull_request")
        issue_data = event_payload.get("issue")

        if pr_data:
            return self._parse_pr_event(event_payload, pr_data, agent_type)
        elif issue_data:
            return self._parse_issue_event(event_payload, issue_data, agent_type)

        log.warning("labeled event has neither issue nor pull_request data")
        return None

    def _parse_pr_event(
        self, event_payload: dict, pr_data: dict, agent_type: AgentType
    ) -> TriggerContext:
        owner, repo = self._extract_repo_info(pr_data)
        server_url = self._extract_server_url(pr_data)

        ctx = TriggerContext(
            agent_type=agent_type,
            event_type="pull_request",
            owner=owner,
            repo=repo,
            issue_number=None,
            pr_number=pr_data.get("number"),
            issue_title=pr_data.get("title", ""),
            issue_body=pr_data.get("body", "") or "",
            creator=pr_data.get("user", {}).get("login", ""),
            server_url=server_url,
            run_id=None,
            trigger_label=event_payload.get("label", {}).get("name", ""),
        )
        log.info(
            "label trigger matched",
            agent_type=agent_type.value,
            pr_number=ctx.pr_number,
            owner=owner,
            repo=repo,
        )
        return ctx

    def _parse_issue_event(
        self, event_payload: dict, issue_data: dict, agent_type: AgentType
    ) -> TriggerContext:
        owner, repo = self._extract_repo_info(issue_data)
        server_url = self._extract_server_url(issue_data)

        ctx = TriggerContext(
            agent_type=agent_type,
            event_type="issues",
            owner=owner,
            repo=repo,
            issue_number=issue_data.get("number"),
            pr_number=None,
            issue_title=issue_data.get("title", ""),
            issue_body=issue_data.get("body", "") or "",
            creator=issue_data.get("user", {}).get("login", ""),
            server_url=server_url,
            run_id=None,
            trigger_label=event_payload.get("label", {}).get("name", ""),
        )
        log.info(
            "label trigger matched",
            agent_type=agent_type.value,
            issue_number=ctx.issue_number,
            owner=owner,
            repo=repo,
        )
        return ctx

    # Pattern for valid GitHub owner/repo names
    _VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

    def _extract_repo_info(self, resource_data: dict) -> tuple[str, str]:
        """Extract and validate owner/repo from a resource's URLs.

        Args:
            resource_data: Issue or PR payload containing URL fields.

        Returns:
            Validated (owner, repo) tuple.

        Raises:
            ValueError: If owner/repo is empty, contains invalid characters,
                or includes path traversal sequences.
        """
        owner, repo = "", ""

        repo_url = resource_data.get("repository_url", "")
        if repo_url:
            # Format: https://<host>/repos/<owner>/<repo>
            parts = repo_url.rstrip("/").split("/")
            if len(parts) >= 2:
                owner, repo = parts[-2], parts[-1]

        if not (owner and repo):
            html_url = resource_data.get("html_url", "")
            if html_url:
                # Format: https://<host>/<owner>/<repo>/...
                parts = html_url.rstrip("/").split("/")
                if len(parts) >= 5:
                    owner, repo = parts[3], parts[4]

        self._validate_repo_name(owner, "owner")
        self._validate_repo_name(repo, "repo")

        return owner, repo

    @classmethod
    def _validate_repo_name(cls, value: str, field: str) -> None:
        """Validate a single owner or repo name component.

        Args:
            value: The name string to validate.
            field: Human-readable field name for error messages.

        Raises:
            ValueError: If the value is empty, contains path traversal
                sequences, or uses disallowed characters.
        """
        if not value:
            raise ValueError(
                f"Webhook payload missing required field: {field} is empty"
            )
        if ".." in value:
            raise ValueError(
                f"Path traversal detected in {field}: {value!r}"
            )
        if not cls._VALID_NAME_RE.match(value):
            raise ValueError(
                f"Invalid characters in {field}: {value!r}. "
                f"Only alphanumeric, '.', '_', and '-' are allowed."
            )

    def _extract_server_url(self, resource_data: dict) -> str:
        """Extract the server base URL from a resource's html_url."""
        html_url = resource_data.get("html_url", "")
        if html_url:
            match = re.match(r"(https?://[^/]+)", html_url)
            if match:
                return match.group(1)
        return ""
