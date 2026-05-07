"""Core coding agent that processes GitHub issues end-to-end.

Reads a GitHub issue, generates code via the Copilot SDK, creates a branch,
commits, pushes, and opens a pull request.
"""

from __future__ import annotations

import os
import re

import structlog

from agent.config import AppConfig
from agent.copilot_session import CopilotSessionManager
from agent.ghes_client import GHESClient
from agent.tools.git_tools import (
    _run_git,
    configure_git_credentials,
    configure_git_user,
    git_add_all,
    git_branch_exists_remote,
    git_checkout_existing_branch,
    git_checkout_new_branch,
    git_commit,
    git_diff,
    git_push,
    git_rev_parse,
    git_status,
)
from agent.triggers.label_trigger import TriggerContext
from agent.utils.prompts import CODER_SYSTEM_PROMPT, PromptManager, localized_text

logger = structlog.get_logger(__name__)

# Maximum characters of repo context to include in prompts
_MAX_CONTEXT_CHARS = 12_000
_MAX_PR_SUMMARY_INPUT_CHARS = 10_000
_PR_SUMMARY_TIMEOUT_SECONDS = 300


class CoderAgent:
    """Autonomous coding agent that resolves GitHub issues via Copilot."""

    def __init__(self) -> None:
        self._log = logger.bind(agent="coder")
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
        """Run the full coder workflow for a single issue.

        If a branch and PR already exist for this issue, collects prior
        comments and review feedback, checks out the existing branch, and
        appends a new commit instead of creating a new PR.

        Returns the URL of the created or updated pull request.
        """
        owner, repo = ctx.owner, ctx.repo
        issue_number = ctx.issue_number
        if issue_number is None:
            raise ValueError("CoderAgent requires an issue number in the trigger context")

        log = self._log.bind(owner=owner, repo=repo, issue=issue_number)
        log.info("coder_agent.start")

        self._prompt_mgr.set_output_language(config.agent.output_language)
        output_language = config.agent.output_language

        # a. Update labels -------------------------------------------------
        trigger_label = self._resolve_trigger_label(config, ctx)
        try:
            await ghes_client.update_issue_labels(
                owner, repo, issue_number,
                add_labels=["in-progress"],
                remove_labels=[trigger_label],
            )
            log.info("labels.updated", added="in-progress", removed=trigger_label)
        except Exception:
            log.warning("labels.update_failed", exc_info=True)

        # b. Detect existing branch & PR -----------------------------------
        branch_name = f"{config.agent.branch_prefix}{issue_number}"
        working_dir = os.getcwd()
        is_followup = False
        existing_pr: dict | None = None

        await configure_git_user(cwd=working_dir)
        await configure_git_credentials(
            config.ghes.hostname, config.ghes.gh_token, cwd=working_dir,
        )

        branch_exists = await git_branch_exists_remote(branch_name, cwd=working_dir)
        if branch_exists:
            existing_pr = await ghes_client.find_pr_by_branch(owner, repo, branch_name)

        if branch_exists and existing_pr:
            is_followup = True
            log.info("followup_mode", branch=branch_name, pr=existing_pr.get("number"))
            await self._post_comment(
                ghes_client, owner, repo, issue_number,
                localized_text(
                    output_language,
                    en=(
                        "Copilot is working on follow-up changes...\n\n"
                        "I'll review the feedback and push additional commits "
                        "to the existing PR."
                    ),
                    ko=(
                        "Copilot이 후속 변경 작업을 시작했습니다.\n\n"
                        "피드백을 확인하고 기존 PR에 추가 커밋을 푸시하겠습니다."
                    ),
                ),
            )
        else:
            await self._post_comment(
                ghes_client, owner, repo, issue_number,
                localized_text(
                    output_language,
                    en=(
                        "Copilot is working on this issue...\n\n"
                        "I'll analyse the repository, implement the changes, "
                        "and open a PR when ready."
                    ),
                    ko=(
                        "Copilot이 이 이슈 작업을 시작했습니다.\n\n"
                        "리포지토리를 분석하고 변경을 구현한 뒤 준비되면 PR을 열겠습니다."
                    ),
                ),
            )

        try:
            # c. Collect context ------------------------------------------------
            log.info("context.collecting")
            repo_context = await self._collect_repo_context(
                ghes_client, owner, repo,
            )
            file_tree = await self._get_remote_file_tree(ghes_client, owner, repo)

            # c-2. Collect conversation history ---------------------------------
            conversation_context = await self._collect_conversation_context(
                ghes_client, owner, repo, issue_number,
                existing_pr.get("number") if existing_pr else None,
            )

            # d. Checkout branch ------------------------------------------------
            log.info("branch.preparing", branch=branch_name, followup=is_followup)
            if is_followup:
                await git_checkout_existing_branch(branch_name, cwd=working_dir)
            else:
                await git_checkout_new_branch(
                    branch_name, config.agent.default_branch, cwd=working_dir,
                )
            log.info("branch.ready", branch=branch_name, working_dir=working_dir)

            # e. Build prompt ---------------------------------------------------
            log.info("copilot.starting")
            prompt = self._prompt_mgr.render_prompt(
                "coder_implement",
                issue_title=ctx.issue_title,
                issue_body=ctx.issue_body or "(no description)",
                repo_context=repo_context,
                file_list=file_tree,
            )

            # Append conversation context if available
            if conversation_context:
                prompt += f"\n\n{conversation_context}"

            # f. Run Copilot ----------------------------------------------------
            head_before = await git_rev_parse("HEAD", cwd=working_dir)
            log.info("copilot.starting", head_before=head_before[:12])

            async with CopilotSessionManager(
                model=config.copilot.coder_model,
                working_dir=working_dir,
            ) as session:
                response = await session.execute(
                    f"{CODER_SYSTEM_PROMPT}\n\n{prompt}"
                )
                log.info("copilot.response_received", length=len(response))

                # Check if Copilot wrote files; if not, nudge once more
                diff_check = await git_diff(cwd=working_dir)
                status_check = await git_status(cwd=working_dir)
                head_check = await git_rev_parse("HEAD", cwd=working_dir)
                if (
                    not diff_check.strip()
                    and not status_check.strip()
                    and head_check == head_before
                ):
                    log.warning("no_changes_after_first_attempt, retrying in same session")
                    response = await session.execute(
                        "You did not write any files to disk. "
                        "You MUST use the file tools (create_file, edit_file) to "
                        "actually create the files now. Do not explain — just write the code."
                    )
                    log.info("copilot.retry_response_received", length=len(response))

            # Check if any changes were actually generated.
            # Copilot may have already committed changes via its own tools,
            # so we check unstaged diff, uncommitted status, AND new commits.
            diff_output = await git_diff(cwd=working_dir)
            status_output = await git_status(cwd=working_dir)
            head_after = await git_rev_parse("HEAD", cwd=working_dir)
            copilot_committed = head_before != head_after

            has_changes = (
                bool(diff_output.strip())
                or bool(status_output.strip())
                or copilot_committed
            )
            log.info(
                "change_detection",
                has_diff=bool(diff_output.strip()),
                has_status=bool(status_output.strip()),
                copilot_committed=copilot_committed,
            )

            if not has_changes:
                log.warning("no_changes_generated")
                await self._post_comment(
                    ghes_client, owner, repo, issue_number,
                    localized_text(
                        output_language,
                        en=(
                            "Copilot analysed the issue but did not generate any code "
                            "changes. This may require manual intervention or a more "
                            "detailed issue description."
                        ),
                        ko=(
                            "Copilot이 이슈를 분석했지만 코드 변경을 생성하지 못했습니다. "
                            "수동 확인이 필요하거나 이슈 설명을 더 구체화해야 할 수 있습니다."
                        ),
                    ),
                )
                await self._update_labels_on_finish(
                    ghes_client, owner, repo, issue_number, success=False,
                )
                return ""

            # g. Commit changes -------------------------------------------------
            # If Copilot already committed, skip our own commit.
            if copilot_committed:
                log.info("commit.already_done_by_copilot", head=head_after[:12])
            else:
                log.info("commit.creating")
                commit_message = self._build_commit_message(ctx)
                await git_add_all(cwd=working_dir)
                await git_commit(commit_message, cwd=working_dir)
                log.info("commit.created")

            # Verify there are actual commits ahead of base before pushing
            final_head = await git_rev_parse("HEAD", cwd=working_dir)
            if final_head == head_before:
                log.warning("no_new_commits_after_processing")
                await self._post_comment(
                    ghes_client, owner, repo, issue_number,
                    localized_text(
                        output_language,
                        en=(
                            "Copilot processed the issue but produced no new commits. "
                            "This may require manual intervention or a more detailed "
                            "issue description."
                        ),
                        ko=(
                            "Copilot이 이슈를 처리했지만 새 커밋이 생성되지 않았습니다. "
                            "수동 확인이 필요하거나 이슈 설명을 더 구체화해야 할 수 있습니다."
                        ),
                    ),
                )
                await self._update_labels_on_finish(
                    ghes_client, owner, repo, issue_number, success=False,
                )
                return ""

            # h. Push branch ----------------------------------------------------
            log.info("push.starting", branch=branch_name)
            await git_push(branch_name, cwd=working_dir)
            log.info("push.completed")

            # i. Create or update PR --------------------------------------------
            if is_followup and existing_pr:
                pr_url = existing_pr.get("html_url", "")
                pr_number = existing_pr.get("number")
                log.info("pr.updated", pr_url=pr_url, pr_number=pr_number)

                await self._post_comment(
                    ghes_client, owner, repo, issue_number,
                    localized_text(
                        output_language,
                        en=(
                            f"Follow-up changes pushed to existing PR: {pr_url}\n\n"
                            "Please review the new commits."
                        ),
                        ko=(
                            f"후속 변경을 기존 PR에 푸시했습니다: {pr_url}\n\n"
                            "새 커밋을 확인해 주세요."
                        ),
                    ),
                )
            else:
                log.info("pr.creating")
                pr_body = await self._build_pr_body(
                    ctx,
                    working_dir,
                    config.agent.default_branch,
                    output_language,
                    config,
                )
                pr = await ghes_client.create_pull_request(
                    owner=owner,
                    repo=repo,
                    title=ctx.issue_title,
                    body=pr_body,
                    head=branch_name,
                    base=config.agent.default_branch,
                    assignees=[ctx.creator] if ctx.creator else None,
                )
                pr_url = pr.get("html_url", "")
                pr_number = pr.get("number")
                log.info("pr.created", pr_url=pr_url, pr_number=pr_number)

                await self._post_comment(
                    ghes_client, owner, repo, issue_number,
                    localized_text(
                        output_language,
                        en=(
                            f"Pull request created: {pr_url}\n\n"
                            "Please review the changes and merge when ready."
                        ),
                        ko=(
                            f"Pull request를 생성했습니다: {pr_url}\n\n"
                            "변경 사항을 검토한 뒤 준비되면 병합해 주세요."
                        ),
                    ),
                )

            # k. Update labels --------------------------------------------------
            await self._update_labels_on_finish(
                ghes_client, owner, repo, issue_number, success=True,
            )

            log.info("coder_agent.completed", pr_url=pr_url)
            return pr_url

        except Exception as exc:
            log.error("coder_agent.failed", error=str(exc), exc_info=True)
            await self._post_comment(
                ghes_client, owner, repo, issue_number,
                localized_text(
                    output_language,
                    en=(
                        "Copilot encountered an error while working on this issue:\n\n"
                        f"```\n{exc}\n```\n\n"
                        "Please check the agent logs for more details."
                    ),
                    ko=(
                        "Copilot이 이 이슈를 처리하는 중 오류가 발생했습니다:\n\n"
                        f"```\n{exc}\n```\n\n"
                        "자세한 내용은 agent 로그를 확인해 주세요."
                    ),
                ),
            )
            await self._update_labels_on_finish(
                ghes_client, owner, repo, issue_number, success=False,
            )
            raise

    # ------------------------------------------------------------------
    # Context collection
    # ------------------------------------------------------------------

    async def _collect_repo_context(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
    ) -> str:
        """Build a formatted string of repository context for the prompt.

        Reads the top-level directory and key project files, truncated to
        fit within token limits.
        """
        log = self._log.bind(owner=owner, repo=repo)
        sections: list[str] = []

        # Top-level file tree
        try:
            entries = await ghes_client.get_directory_contents(owner, repo)
            tree_lines = [
                f"{'[dir]' if e.get('type') == 'dir' else '[file]'} {e.get('name', '')}"
                for e in entries
            ]
            sections.append("### Repository Structure\n" + "\n".join(tree_lines))
        except Exception:
            log.warning("context.file_tree_failed", exc_info=True)

        # Key project files
        key_files = [
            "README.md",
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "Makefile",
            ".github/workflows",
        ]

        for filepath in key_files:
            try:
                content = await ghes_client.get_file_content(owner, repo, filepath)
                if content:
                    # Truncate individual files to keep total context manageable
                    truncated = content[:3000]
                    if len(content) > 3000:
                        truncated += "\n... (truncated)"
                    sections.append(f"### {filepath}\n```\n{truncated}\n```")
            except Exception:
                # File doesn't exist or isn't readable – skip silently
                pass

        context = "\n\n".join(sections)

        # Enforce total context size limit
        if len(context) > _MAX_CONTEXT_CHARS:
            context = context[:_MAX_CONTEXT_CHARS] + "\n\n... (context truncated)"

        log.info("context.collected", length=len(context))
        return context

    # ------------------------------------------------------------------
    # Conversation context collection
    # ------------------------------------------------------------------

    async def _collect_conversation_context(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        issue_number: int,
        pr_number: int | None,
    ) -> str:
        """Collect issue comments and PR review comments for context.

        Returns a formatted string with conversation history, or empty
        string if no relevant context is found.
        """
        log = self._log.bind(issue=issue_number, pr=pr_number)
        sections: list[str] = []

        # Collect issue comments
        try:
            comments = await ghes_client.get_issue_comments(owner, repo, issue_number)
            if comments:
                comment_lines: list[str] = []
                for c in comments[-10:]:  # Last 10 comments
                    user = c.get("user", {}).get("login", "unknown")
                    body = c.get("body", "")[:3000]
                    comment_lines.append(f"**@{user}**: {body}")
                sections.append(
                    "## Issue Comments (recent)\n" + "\n\n".join(comment_lines)
                )
                log.info("conversation.issue_comments", count=len(comments))
        except Exception:
            log.warning("conversation.issue_comments_failed", exc_info=True)

        # Collect PR review comments if PR exists
        if pr_number:
            try:
                review_comments = await ghes_client.get_pr_review_comments(
                    owner, repo, pr_number,
                )
                if review_comments:
                    review_lines: list[str] = []
                    for rc in review_comments[-10:]:  # Last 10 review comments
                        user = rc.get("user", {}).get("login", "unknown")
                        path = rc.get("path", "")
                        body = rc.get("body", "")[:3000]
                        review_lines.append(f"**@{user}** on `{path}`: {body}")
                    sections.append(
                        "## PR Review Comments\n" + "\n\n".join(review_lines)
                    )
                    log.info("conversation.pr_reviews", count=len(review_comments))
            except Exception:
                log.warning("conversation.pr_reviews_failed", exc_info=True)

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _build_commit_message(self, ctx: TriggerContext) -> str:
        """Build a conventional commit message with co-author trailer."""
        # Determine commit type from issue title heuristics
        title_lower = ctx.issue_title.lower()
        if any(kw in title_lower for kw in ("bug", "fix", "error", "crash")):
            prefix = "fix"
        elif any(kw in title_lower for kw in ("refactor", "clean", "reorgan")):
            prefix = "refactor"
        elif any(kw in title_lower for kw in ("test",)):
            prefix = "test"
        elif any(kw in title_lower for kw in ("doc", "readme")):
            prefix = "docs"
        else:
            prefix = "feat"

        message = f"{prefix}: {ctx.issue_title} (#{ctx.issue_number})"

        if ctx.creator:
            message += f"\n\nCo-authored-by: {ctx.creator} <{ctx.creator}@users.noreply.github.com>"

        return message

    async def _build_pr_body(
        self,
        ctx: TriggerContext,
        working_dir: str,
        base_branch: str,
        output_language: str,
        config: AppConfig | None = None,
    ) -> str:
        """Build the pull request description body.

        Uses ground-truth git data against ``origin/<base>`` and, when
        configured, a lightweight model call to produce reviewer-friendly
        summary text.
        """
        base_ref = f"origin/{base_branch}"

        try:
            name_status = await _run_git(
                "diff", "--name-status", f"{base_ref}...HEAD",
                cwd=working_dir,
            )
        except Exception:
            name_status = ""

        try:
            diff_stat = await _run_git(
                "diff", "--stat", f"{base_ref}...HEAD",
                cwd=working_dir,
            )
        except Exception:
            diff_stat = ""

        try:
            shortstat = await _run_git(
                "diff", "--shortstat", f"{base_ref}...HEAD",
                cwd=working_dir,
            )
        except Exception:
            shortstat = ""

        try:
            commit_log = await _run_git(
                "log", f"{base_ref}..HEAD", "--format=%s",
                cwd=working_dir,
            )
        except Exception:
            commit_log = ""

        file_entries = self._parse_name_status(name_status)
        issue_excerpt = (ctx.issue_body or "").strip()
        if len(issue_excerpt) > 1200:
            issue_excerpt = issue_excerpt[:1200].rstrip() + "…"

        generated_summary = None
        if config is not None and config.copilot.coder_pr_summary_model:
            generated_summary = await self._generate_pr_summary(
                ctx=ctx,
                config=config,
                output_language=output_language,
                issue_excerpt=issue_excerpt,
                commit_log=commit_log,
                diff_stat=diff_stat,
                name_status=name_status,
            )

        if generated_summary is None:
            generated_summary = self._fallback_pr_summary(ctx, output_language)

        summary_text = generated_summary
        footprint_block = self._build_change_footprint(
            file_entries, shortstat, output_language,
        )

        sections = [
            f"Closes #{ctx.issue_number}",
            "",
            localized_text(output_language, en="## Summary", ko="## 요약"),
            "",
            summary_text,
            "",
            localized_text(
                output_language,
                en="## Change Footprint",
                ko="## 변경 규모",
            ),
            "",
            footprint_block,
            "",
            "---",
            localized_text(
                output_language,
                en="*Generated by GHES Coding Agent*",
                ko="*GHES Coding Agent가 생성했습니다*",
            ),
        ]
        return "\n".join(sections)

    async def _generate_pr_summary(
        self,
        *,
        ctx: TriggerContext,
        config: AppConfig,
        output_language: str,
        issue_excerpt: str,
        commit_log: str,
        diff_stat: str,
        name_status: str,
    ) -> str | None:
        """Generate a PR summary with a lightweight model, returning None on failure."""
        self._prompt_mgr.set_output_language(output_language)
        prompt = self._prompt_mgr.render_prompt(
            "coder_pr_summary",
            issue_title=ctx.issue_title,
            issue_body_excerpt=issue_excerpt or "(no issue body provided)",
            commit_subjects=self._truncate_pr_summary_input(commit_log or "(none)"),
            diff_stat=self._truncate_pr_summary_input(diff_stat or "(no diff stat)"),
            name_status_summary=self._truncate_pr_summary_input(
                name_status or "(no changed file status)"
            ),
        )
        timeout = min(config.agent.timeout_minutes * 60, _PR_SUMMARY_TIMEOUT_SECONDS)

        try:
            async with CopilotSessionManager(
                model=config.copilot.coder_pr_summary_model,
                timeout=timeout,
            ) as session:
                raw_summary = await session.execute(prompt)
        except Exception:
            self._log.warning("pr_summary_generation_failed", exc_info=True)
            return None

        parsed = self._parse_pr_summary_response(raw_summary)
        if parsed is None:
            self._log.warning(
                "pr_summary_parse_failed",
                raw_preview=(raw_summary or "").strip()[:300],
            )
        return parsed

    @staticmethod
    def _parse_pr_summary_response(raw_summary: str) -> str | None:
        """Parse the lightweight PR summary response into a summary string."""
        text = (raw_summary or "").strip()
        if not text:
            return None

        summary_match = re.search(
            r"(?ims)^###\s+SUMMARY\s*(.*?)(?=^###\s+|\Z)",
            text,
        )
        if not summary_match:
            return None

        summary = CoderAgent._clean_pr_summary_section(summary_match.group(1), 1600)
        if not summary:
            return None
        return summary

    @staticmethod
    def _clean_pr_summary_section(text: str, limit: int) -> str:
        """Clean and cap model-authored PR body sections."""
        cleaned = text.strip()
        cleaned = re.sub(r"(?m)^```[a-zA-Z0-9_-]*\s*$", "", cleaned)
        cleaned = re.sub(r"(?m)^```\s*$", "", cleaned).strip()
        if len(cleaned) > limit:
            cleaned = cleaned[:limit].rstrip() + "…"
        return cleaned

    @staticmethod
    def _fallback_pr_summary(
        ctx: TriggerContext,
        output_language: str,
    ) -> str:
        """Build deterministic PR summary text when model generation is unavailable."""
        return localized_text(
            output_language,
            en=f"This PR addresses **{ctx.issue_title}**.",
            ko=f"이 PR은 **{ctx.issue_title}** 이슈를 처리합니다.",
        )

    @staticmethod
    def _parse_name_status(name_status: str) -> list[tuple[str, str]]:
        """Parse ``git diff --name-status`` output into ``(status, path)`` tuples."""
        entries: list[tuple[str, str]] = []
        for line in name_status.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0][:1]
            path = parts[-1]
            entries.append((status, path))
        return entries

    @staticmethod
    def _build_change_footprint(
        file_entries: list[tuple[str, str]],
        shortstat: str,
        output_language: str,
    ) -> str:
        """Build a compact deterministic footprint instead of a full file list."""
        if not file_entries:
            return localized_text(
                output_language,
                en="- No file-level diff was detected.",
                ko="- 파일 단위 diff를 찾지 못했습니다.",
            )

        counts: dict[str, int] = {}
        areas: dict[str, int] = {}
        for status, path in file_entries:
            counts[status] = counts.get(status, 0) + 1
            area = CoderAgent._path_area(path)
            areas[area] = areas.get(area, 0) + 1

        total = len(file_entries)
        lines = [
            localized_text(
                output_language,
                en=f"- Changed {total} file(s).",
                ko=f"- {total}개 파일을 변경했습니다.",
            )
        ]
        if shortstat.strip():
            lines.append(f"- `{shortstat.strip()}`")

        breakdown = CoderAgent._format_status_breakdown(counts, output_language)
        if breakdown:
            lines.append(breakdown)

        area_text = CoderAgent._format_main_areas(areas, output_language)
        if area_text:
            lines.append(area_text)

        lines.append(
            localized_text(
                output_language,
                en="- See the **Files changed** tab for the full file-by-file diff.",
                ko="- 전체 파일별 diff는 **Files changed** 탭에서 확인하세요.",
            )
        )
        return "\n".join(lines)

    @staticmethod
    def _format_status_breakdown(counts: dict[str, int], output_language: str) -> str:
        """Format changed-file status counts as a compact bullet."""
        labels = {
            "A": localized_text(output_language, en="added", ko="추가"),
            "M": localized_text(output_language, en="modified", ko="수정"),
            "D": localized_text(output_language, en="deleted", ko="삭제"),
            "R": localized_text(output_language, en="renamed", ko="이름 변경"),
            "C": localized_text(output_language, en="copied", ko="복사"),
        }
        parts = [
            f"{labels.get(status, status)} {count}"
            for status, count in sorted(counts.items())
        ]
        if not parts:
            return ""
        prefix = localized_text(output_language, en="Breakdown", ko="구성")
        return f"- {prefix}: {', '.join(parts)}."

    @staticmethod
    def _format_main_areas(areas: dict[str, int], output_language: str) -> str:
        """Format the most common path areas touched by the PR."""
        if not areas:
            return ""
        ordered = sorted(areas.items(), key=lambda item: (-item[1], item[0]))
        shown = [area for area, _ in ordered[:5]]
        remaining = len(ordered) - len(shown)
        if remaining > 0:
            shown.append(
                localized_text(
                    output_language,
                    en=f"and {remaining} more",
                    ko=f"외 {remaining}개",
                )
            )
        prefix = localized_text(output_language, en="Main areas", ko="주요 영역")
        return f"- {prefix}: {', '.join(shown)}."

    @staticmethod
    def _path_area(path: str) -> str:
        """Return a compact area label for a changed file path."""
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == ".github":
            return "/".join(parts[:2])
        if len(parts) >= 2:
            return parts[0]
        return path or "."

    @staticmethod
    def _truncate_pr_summary_input(text: str) -> str:
        """Keep lightweight PR summary prompts bounded."""
        if len(text) <= _MAX_PR_SUMMARY_INPUT_CHARS:
            return text
        return text[:_MAX_PR_SUMMARY_INPUT_CHARS].rstrip() + "\n... (truncated)"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_remote_file_tree(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
    ) -> list[str]:
        """Get the top-level file/directory names from the remote repo."""
        try:
            entries = await ghes_client.get_directory_contents(owner, repo)
            return [e.get("name", "") for e in entries if e.get("name")]
        except Exception:
            self._log.warning("file_tree.remote_fetch_failed", exc_info=True)
            return []

    def _resolve_trigger_label(self, config: AppConfig, ctx: TriggerContext) -> str:
        """Return the trigger label name to remove from the issue."""
        if ctx.trigger_label:
            return ctx.trigger_label
        labels = config.agent.labels
        if labels:
            return next(iter(labels))
        return "copilot"

    async def _post_comment(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        issue_number: int,
        body: str,
    ) -> None:
        """Post a comment on the issue, logging but not raising on failure."""
        try:
            await ghes_client.create_issue_comment(owner, repo, issue_number, body)
            self._log.info("comment.posted", issue=issue_number)
        except Exception:
            self._log.warning("comment.post_failed", issue=issue_number, exc_info=True)

    async def _update_labels_on_finish(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        success: bool,
    ) -> None:
        """Update labels to reflect final status."""
        add = ["ready-for-review"] if success else ["agent-error"]
        remove = ["in-progress", "copilot"]
        try:
            await ghes_client.update_issue_labels(
                owner, repo, issue_number,
                add_labels=add,
                remove_labels=remove,
            )
            self._log.info("labels.final_update", added=add, removed=remove)
        except Exception:
            self._log.warning("labels.final_update_failed", exc_info=True)
