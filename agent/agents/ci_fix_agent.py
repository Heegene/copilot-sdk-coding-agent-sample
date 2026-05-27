"""CI failure auto-fix agent.

Monitors failed workflow runs, diagnoses errors from logs, generates fixes
via the Copilot SDK, and pushes corrective commits to the PR branch.
"""

from __future__ import annotations

import io
import os
import re
import zipfile

import structlog

from agent.config import AppConfig
from agent.copilot_session import CopilotSessionManager
from agent.ghes_client import GHESClient
from agent.tools.git_tools import (
    git_add_all,
    git_checkout_existing_branch,
    git_commit,
    git_diff,
    git_push,
    git_rev_parse,
)
from agent.triggers.label_trigger import TriggerContext
from agent.utils.prompts import PromptManager, localized_text

logger = structlog.get_logger(__name__)

_MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024   # 50 MB per file
_MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024   # 200 MB total extracted
_MAX_ATTEMPTS = 3
_LOG_TAIL_LINES = 200


class CIFixAgent:
    """Automatically diagnoses and fixes CI failures on a pull request."""

    def __init__(self) -> None:
        self._log = logger.bind(agent="ci_fix")
        self._prompt_mgr = PromptManager()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def execute(
        self,
        ctx: TriggerContext,
        ghes_client: GHESClient,
        config: AppConfig,
    ) -> str:
        """Attempt to fix CI failures, retrying up to 3 times."""
        owner, repo = ctx.owner, ctx.repo
        pr_number = ctx.pr_number
        output_language = config.agent.output_language
        if pr_number is None:
            log = self._log.bind(owner=owner, repo=repo)
            log.warning("ci_fix_agent.no_pr_number", reason="PR could not be resolved")
            return localized_text(
                output_language,
                en="CIFixAgent skipped: could not resolve a PR number.",
                ko="CIFixAgent를 건너뛰었습니다: PR 번호를 확인할 수 없습니다.",
            )

        log = self._log.bind(owner=owner, repo=repo, pr=pr_number)
        log.info("ci_fix_agent.start")

        self._prompt_mgr.set_output_language(output_language)

        # a. Resolve the failed workflow run --------------------------------
        run_id = await self._resolve_run_id(ctx, ghes_client, owner, repo, pr_number)
        if run_id is None:
            msg = localized_text(
                output_language,
                en="Could not find a failed workflow run for this PR.",
                ko="이 PR에서 실패한 workflow run을 찾을 수 없습니다.",
            )
            await self._post_comment(ghes_client, owner, repo, pr_number, msg)
            return msg

        # Get PR details for branch name
        pr_data = await ghes_client.get_pull_request(owner, repo, pr_number)
        branch_name = pr_data["head"]["ref"]
        working_dir = os.getcwd()
        await git_checkout_existing_branch(branch_name, cwd=working_dir)

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            log.info("ci_fix.attempt", attempt=attempt)

            try:
                # b. Download workflow run logs ---------------------------------
                log_bytes = await ghes_client.get_workflow_run_logs(owner, repo, run_id)

                # c. List failed jobs -------------------------------------------
                jobs = await ghes_client.list_workflow_run_jobs(owner, repo, run_id)
                failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

                if not failed_jobs:
                    log.info("ci_fix.no_failed_jobs")
                    msg = localized_text(
                        output_language,
                        en="No failed jobs found - CI may have recovered.",
                        ko="실패한 job을 찾지 못했습니다. CI가 이미 복구되었을 수 있습니다.",
                    )
                    await self._post_comment(ghes_client, owner, repo, pr_number, msg)
                    return msg

                # d. Extract error messages from logs ---------------------------
                error_context = self._extract_errors_from_logs(log_bytes, failed_jobs)

                # e. Collect relevant source files ------------------------------
                file_context = await self._collect_file_context(
                    error_context, ghes_client, owner, repo,
                )

                # f. Send CI_FIX_PROMPT to Copilot ------------------------------
                failed_step_names = ", ".join(
                    j.get("name", "unknown") for j in failed_jobs
                )
                prompt = self._prompt_mgr.render_prompt(
                    "ci_fix",
                    failed_step=failed_step_names,
                    error_logs=error_context["combined_logs"],
                    file_context=file_context,
                )

                head_before = await git_rev_parse("HEAD", cwd=working_dir)

                async with CopilotSessionManager(
                    model=config.copilot.coder_model,
                    timeout=config.agent.timeout_minutes * 60,
                    working_dir=working_dir,
                ) as session:
                    response = await session.execute(prompt)
                    log.info("copilot.response_received", length=len(response))

                # g. Commit the fix ---------------------------------------------
                await git_add_all(cwd=working_dir)
                staged_diff = await git_diff(staged=True, cwd=working_dir)
                head_after = await git_rev_parse("HEAD", cwd=working_dir)
                if staged_diff.strip():
                    await git_commit(
                        f"fix: resolve CI failure (attempt #{attempt})",
                        cwd=working_dir,
                    )
                elif head_after == head_before:
                    msg = localized_text(
                        output_language,
                        en=(
                            f"CI fix attempt #{attempt} analysed the failure, "
                            "but generated no file changes."
                        ),
                        ko=(
                            f"CI fix attempt #{attempt}에서 실패 원인을 분석했지만 "
                            "파일 변경은 생성되지 않았습니다."
                        ),
                    )
                    await self._post_comment(ghes_client, owner, repo, pr_number, msg)
                    return msg

                # h. Push to the same branch ------------------------------------
                await git_push(branch_name, cwd=working_dir)
                log.info("ci_fix.pushed", attempt=attempt, branch=branch_name)

                # i. Post comment on PR -----------------------------------------
                await self._post_comment(
                    ghes_client, owner, repo, pr_number,
                    localized_text(
                        output_language,
                        en=f"CI fix attempt #{attempt} pushed to `{branch_name}`.",
                        ko=f"CI fix attempt #{attempt} 변경을 `{branch_name}`에 푸시했습니다.",
                    ),
                )

                # j. The fix is pushed; CI will re-run automatically.
                #    If this isn't the last attempt, we'll rely on being
                #    re-triggered by a subsequent workflow_run failure event.
                log.info("ci_fix.attempt_complete", attempt=attempt)
                return localized_text(
                    output_language,
                    en=f"CI fix attempt #{attempt} pushed successfully.",
                    ko=f"CI fix attempt #{attempt} 변경을 성공적으로 푸시했습니다.",
                )

            except Exception as exc:
                log.error("ci_fix.attempt_failed", attempt=attempt, error=str(exc), exc_info=True)
                if attempt == _MAX_ATTEMPTS:
                    break

        # k. All attempts exhausted -----------------------------------------
        msg = localized_text(
            output_language,
            en=(
                "Could not auto-fix CI. Manual intervention needed.\n\n"
                f"Tried {_MAX_ATTEMPTS} fix attempts without success."
            ),
            ko=(
                "CI를 자동으로 수정하지 못했습니다. 수동 확인이 필요합니다.\n\n"
                f"{_MAX_ATTEMPTS}번의 수정 시도가 모두 실패했습니다."
            ),
        )
        await self._post_comment(ghes_client, owner, repo, pr_number, msg)
        log.warning("ci_fix_agent.exhausted", attempts=_MAX_ATTEMPTS)
        return msg

    # ------------------------------------------------------------------
    # Run resolution
    # ------------------------------------------------------------------

    async def _resolve_run_id(
        self,
        ctx: TriggerContext,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> int | None:
        """Get the failed workflow run ID from context or find the latest."""
        if ctx.run_id:
            return int(ctx.run_id)

        # Fallback: look up the PR head branch and find the latest failed run
        pr_data = await ghes_client.get_pull_request(owner, repo, pr_number)
        head_sha = pr_data.get("head", {}).get("sha")
        if not head_sha:
            return None

        # Use the check-runs API to find a failed run
        try:
            data = await ghes_client._request(
                "GET",
                f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            )
            for check_run in data.get("check_runs", []):
                if check_run.get("conclusion") == "failure":
                    # Extract run_id from the details_url if available
                    details_url = check_run.get("details_url", "")
                    match = re.search(r"/runs/(\d+)", details_url)
                    if match:
                        return int(match.group(1))
        except Exception:
            self._log.warning("run_id.lookup_failed", exc_info=True)

        return None

    # ------------------------------------------------------------------
    # Log parsing
    # ------------------------------------------------------------------

    def _extract_errors_from_logs(
        self,
        log_bytes: bytes,
        failed_jobs: list[dict],
    ) -> dict:
        """Extract error messages from workflow log zip archive.

        Returns a dict with 'combined_logs' (str) and 'file_refs' (set of
        file paths mentioned in errors).
        """
        combined_lines: list[str] = []
        file_refs: set[str] = set()

        try:
            with zipfile.ZipFile(io.BytesIO(log_bytes)) as zf:
                total_extracted = 0
                for name in zf.namelist():
                    if not name.endswith(".txt"):
                        continue
                    info = zf.getinfo(name)
                    if info.file_size > _MAX_ZIP_MEMBER_BYTES:
                        self._log.warning(
                            "logs.member_too_large",
                            name=name,
                            size=info.file_size,
                        )
                        continue
                    if total_extracted + info.file_size > _MAX_ZIP_TOTAL_BYTES:
                        self._log.warning(
                            "logs.total_size_exceeded",
                            name=name,
                            total=total_extracted,
                        )
                        break
                    content = zf.read(name).decode(errors="replace")
                    total_extracted += info.file_size
                    lines = content.splitlines()
                    # Take the last N lines from each log file
                    tail = lines[-_LOG_TAIL_LINES:]
                    combined_lines.extend(tail)

                    # Extract file references from error lines
                    for line in tail:
                        refs = re.findall(
                            r'(?:File\s+["\']|(?:at\s+))?'
                            r'([\w./\\-]+\.(?:py|js|ts|go|java|rs|rb))'
                            r'(?:[:\s,]|$)',
                            line,
                        )
                        file_refs.update(refs)
        except zipfile.BadZipFile:
            self._log.warning("logs.bad_zip")
            # Treat raw bytes as plain text
            text = log_bytes.decode(errors="replace")
            combined_lines = text.splitlines()[-_LOG_TAIL_LINES:]

        return {
            "combined_logs": "\n".join(combined_lines[-500:]),  # cap total
            "file_refs": file_refs,
        }

    # ------------------------------------------------------------------
    # File context collection
    # ------------------------------------------------------------------

    async def _collect_file_context(
        self,
        error_context: dict,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
    ) -> list[dict]:
        """Fetch source files referenced in error logs."""
        file_context: list[dict] = []
        for path in list(error_context["file_refs"])[:10]:  # limit
            try:
                content = await ghes_client.get_file_content(owner, repo, path)
                ext = path.rsplit(".", 1)[-1] if "." in path else ""
                file_context.append({
                    "path": path,
                    "language": ext,
                    "content": content[:5000],
                })
            except Exception:
                self._log.debug("file_context.unavailable", path=path)
        return file_context

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _post_comment(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> None:
        """Post a comment on the PR, logging but not raising on failure."""
        try:
            await ghes_client.create_pr_comment(owner, repo, pr_number, body)
            self._log.info("comment.posted", pr=pr_number)
        except Exception:
            self._log.warning("comment.post_failed", pr=pr_number, exc_info=True)
