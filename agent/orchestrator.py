"""Main orchestrator for the GHES Coding Agent.

Entry point that receives GitHub Actions event payloads, routes to the
appropriate agent, and manages the execution lifecycle.

NOTE: Callers (GitHub Actions workflows) should configure concurrency groups
to prevent parallel runs for the same issue/PR/branch.  For example:
    concurrency:
      group: copilot-${{ github.event.issue.number || github.event.pull_request.number }}
      cancel-in-progress: false
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback

import aiofiles
import structlog

from agent.config import AppConfig, load_repo_config
from agent.ghes_client import GHESClient
from agent.triggers.label_trigger import AgentType, LabelTrigger, TriggerContext

logger = structlog.get_logger(__name__)


class Orchestrator:
    """Routes GitHub Actions events to the appropriate agent."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.ghes_client: GHESClient | None = None
        self.label_trigger = LabelTrigger()
        self._pending_head_branch: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self, event_path: str | None = None, *, mode: str = "auto"
    ) -> None:
        """Load event payload, detect trigger, and dispatch to the right agent.

        Args:
            event_path: Path to the JSON event payload file.
            mode: Execution mode — ``"auto"`` (default, event-based routing)
                or ``"ci-fix"`` (read workflow_run event directly).
        """
        event_path = event_path or os.environ.get("GITHUB_EVENT_PATH", "")
        event_name = os.environ.get("GITHUB_EVENT_NAME", "")

        if not event_path:
            logger.error("GITHUB_EVENT_PATH not set")
            sys.exit(1)

        logger.info(
            "loading event", path=event_path, event_name=event_name, mode=mode
        )

        async with aiofiles.open(event_path) as f:
            raw = await f.read()
            event_payload: dict = json.loads(raw)

        ctx: TriggerContext | None = None

        if mode == "ci-fix":
            ctx = self._build_ci_fix_context(event_payload)
        else:
            # Auto mode: try triggers in order
            if event_name in ("issues", "pull_request"):
                ctx = self.label_trigger.parse(event_payload)
            if ctx is None and event_name == "workflow_run":
                ctx = self._build_ci_fix_context_if_applicable(event_payload)

        if ctx is None:
            logger.info("no trigger matched — nothing to do", event_name=event_name)
            return

        # Populate run_id from environment
        ctx.run_id = ctx.run_id or os.environ.get("GITHUB_RUN_ID")

        # Initialise the API client and enter its async context
        ghes_client = GHESClient(
            host=self.config.ghes.hostname,
            token=self.config.ghes.gh_token,
            api_base=self.config.ghes.api_base_url,
        )

        async with ghes_client as client:
            self.ghes_client = client

            self.config = await load_repo_config(
                client, f"{ctx.owner}/{ctx.repo}", self.config,
            )

            # Resolve PR number via API if branch-based extraction failed
            if (
                ctx.agent_type == AgentType.CI_FIX
                and ctx.pr_number is None
                and self._pending_head_branch
            ):
                ctx.pr_number = await self._resolve_pr_from_branch(
                    ctx.owner, ctx.repo, self._pending_head_branch,
                )

            try:
                await self._post_progress(
                    ctx, "🤖 Agent starting... analyzing your request."
                )

                result = await self._route_agent(ctx)

                summary = result if isinstance(result, str) and result else "Done."
                await self._post_progress(ctx, summary)
            except Exception:
                logger.exception("agent execution failed")
                await self._post_error(ctx, traceback.format_exc())
                raise

    # ------------------------------------------------------------------
    # Context builders for non-label/comment triggers
    # ------------------------------------------------------------------

    def _build_ci_fix_context(self, event_payload: dict) -> TriggerContext:
        """Build a CI-fix ``TriggerContext`` from a workflow_run event payload."""
        wf_run = event_payload.get("workflow_run", event_payload)
        repo_data = wf_run.get("repository", event_payload.get("repository", {}))
        full_name = repo_data.get("full_name", os.environ.get("GITHUB_REPOSITORY", "/"))
        owner, repo = full_name.split("/", 1) if "/" in full_name else ("", "")
        html_url = repo_data.get("html_url", "")
        server_url = html_url.rsplit("/", 2)[0] if html_url else ""

        run_id = str(wf_run.get("id", os.environ.get("FAILED_RUN_ID", "")))

        # Attempt to resolve PR number from branch name
        head_branch: str = wf_run.get("head_branch", "")
        pr_number = self._extract_pr_from_branch(head_branch)
        self._pending_head_branch = head_branch

        return TriggerContext(
            agent_type=AgentType.CI_FIX,
            event_type="workflow_run",
            owner=owner,
            repo=repo,
            issue_number=None,
            pr_number=pr_number,
            issue_title=f"CI Fix: {wf_run.get('name', 'workflow')} #{run_id}",
            issue_body=f"Automated CI fix for failed run {run_id}",
            creator="github-actions[bot]",
            server_url=server_url,
            run_id=run_id,
        )

    def _build_ci_fix_context_if_applicable(
        self, event_payload: dict
    ) -> TriggerContext | None:
        """Build a CI-fix context only when the workflow_run failed on a copilot/ branch."""
        wf_run = event_payload.get("workflow_run", {})
        conclusion = wf_run.get("conclusion", "")
        branch = wf_run.get("head_branch", "")
        if conclusion == "failure" and branch.startswith("copilot/"):
            return self._build_ci_fix_context(event_payload)
        logger.debug(
            "workflow_run not applicable for ci-fix",
            conclusion=conclusion,
            branch=branch,
        )
        return None

    @staticmethod
    def _extract_pr_from_branch(branch: str) -> int | None:
        """Extract PR number from a ``copilot/{number}`` branch name."""
        match = re.match(r"^copilot/(\d+)$", branch)
        return int(match.group(1)) if match else None

    async def _resolve_pr_from_branch(
        self,
        owner: str,
        repo: str,
        head_branch: str,
    ) -> int | None:
        """Query GHES API for an open PR with the given head branch."""
        if self.ghes_client is None:
            return None
        try:
            data: list[dict] = await self.ghes_client._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls",
                params={"head": f"{owner}:{head_branch}", "state": "open"},
            )
            if data:
                return int(data[0]["number"])
        except Exception:
            logger.warning(
                "pr_resolution.api_failed",
                branch=head_branch,
                exc_info=True,
            )
        return None

    # ------------------------------------------------------------------
    # Agent routing
    # ------------------------------------------------------------------

    async def _route_agent(self, ctx: TriggerContext) -> str:
        """Dispatch to the agent indicated by *ctx.agent_type*."""
        handlers = {
            AgentType.CODER: self._run_coder,
            AgentType.REVIEWER: self._run_reviewer,
            AgentType.DOC_GEN: self._run_doc_gen,
            AgentType.CI_FIX: self._run_ci_fix,
        }
        handler = handlers.get(ctx.agent_type)
        if handler is None:
            msg = f"Unsupported agent type: {ctx.agent_type}"
            logger.warning(msg)
            return f"⚠️ {msg}"

        logger.info("routing to agent", agent_type=ctx.agent_type.value)
        return await handler(ctx)

    # ------------------------------------------------------------------
    # Individual agent runners (lazy imports)
    # ------------------------------------------------------------------

    async def _run_coder(self, ctx: TriggerContext) -> str:
        from agent.agents.coder_agent import CoderAgent  # type: ignore[import-untyped]

        agent = CoderAgent()
        return await agent.execute(ctx, self.ghes_client, self.config)

    async def _run_reviewer(self, ctx: TriggerContext) -> str:
        from agent.agents.reviewer_agent import ReviewerAgent  # type: ignore[import-untyped]

        agent = ReviewerAgent()
        return await agent.execute(ctx, self.ghes_client, self.config)

    async def _run_doc_gen(self, ctx: TriggerContext) -> str:
        from agent.agents.doc_gen_agent import DocGenAgent  # type: ignore[import-untyped]

        agent = DocGenAgent()
        return await agent.execute(ctx, self.ghes_client, self.config)

    async def _run_ci_fix(self, ctx: TriggerContext) -> str:
        from agent.agents.ci_fix_agent import CIFixAgent  # type: ignore[import-untyped]

        agent = CIFixAgent()
        return await agent.execute(ctx, self.ghes_client, self.config)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _post_progress(self, ctx: TriggerContext, message: str) -> None:
        """Post a progress comment on the associated issue or PR."""
        if self.ghes_client is None:
            return
        number = ctx.pr_number or ctx.issue_number
        if number is None:
            logger.warning("no issue/PR number available for progress comment")
            return
        try:
            await self.ghes_client.create_issue_comment(
                ctx.owner, ctx.repo, number, message
            )
        except Exception:
            logger.exception("failed to post progress comment")

    async def _post_error(self, ctx: TriggerContext, error: str) -> None:
        """Post an error comment with a link to the workflow run."""
        if self.ghes_client is None:
            return
        number = ctx.pr_number or ctx.issue_number
        if number is None:
            logger.warning("no issue/PR number available for error comment")
            return

        run_url = ""
        if ctx.run_id and ctx.server_url:
            repo_slug = os.environ.get("GITHUB_REPOSITORY", f"{ctx.owner}/{ctx.repo}")
            run_url = (
                f"\n\n[View workflow run]"
                f"({ctx.server_url}/{repo_slug}/actions/runs/{ctx.run_id})"
            )

        body = (
            f"❌ **Agent failed**\n\n"
            f"```\n{error[-3000:]}\n```"
            f"{run_url}"
        )
        try:
            await self.ghes_client.create_issue_comment(
                ctx.owner, ctx.repo, number, body
            )
        except Exception:
            logger.exception("failed to post error comment")


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def main() -> None:
    """CLI entry point.

    Supports ``--mode`` flag:
    * ``auto`` (default) — event-based routing via label/workflow_run triggers.
    * ``ci-fix`` — read a ``workflow_run`` event and route directly to the CI-fix agent.
    """
    parser = argparse.ArgumentParser(
        description="GHES Coding Agent orchestrator",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "ci-fix"],
        default="auto",
        help="Execution mode (default: auto)",
    )
    args = parser.parse_args()

    config = AppConfig.from_env()
    orchestrator = Orchestrator(config)
    try:
        asyncio.run(orchestrator.run(mode=args.mode))
    except Exception:
        logger.exception("orchestrator failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
