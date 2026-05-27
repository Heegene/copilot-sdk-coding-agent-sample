"""Documentation generation agent.

Analyses source files and existing docs, generates or updates documentation
via the Copilot SDK, and pushes the result as a pull request.
"""

from __future__ import annotations

import os

import structlog

from agent.config import AppConfig
from agent.copilot_session import CopilotSessionManager
from agent.ghes_client import GHESClient
from agent.tools.git_tools import (
    git_add_all,
    git_branch_exists_remote,
    git_checkout_existing_branch,
    git_checkout_new_branch,
    git_commit,
    git_diff,
    git_push,
    git_rev_parse,
)
from agent.triggers.label_trigger import TriggerContext
from agent.utils.prompts import PromptManager, localized_text

logger = structlog.get_logger(__name__)

# Well-known documentation files to collect as context.
_DOC_FILES = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "docs/README.md",
    "docs/API.md",
    "API.md",
]


class DocGenAgent:
    """Generates or updates project documentation and pushes a PR."""

    def __init__(self) -> None:
        self._log = logger.bind(agent="doc_gen")
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
        """Generate or update documentation for the specified scope."""
        owner, repo = ctx.owner, ctx.repo
        pr_number = ctx.pr_number
        issue_number = ctx.issue_number
        comment_target = pr_number or issue_number
        if comment_target is None:
            raise ValueError("DocGenAgent requires a PR or issue context")

        log = self._log.bind(owner=owner, repo=repo, target=comment_target)
        log.info("doc_gen_agent.start")

        self._prompt_mgr.set_output_language(config.agent.output_language)
        output_language = config.agent.output_language

        await self._post_comment(
            ghes_client, owner, repo, comment_target,
            localized_text(
                output_language,
                en=(
                    "**Documentation generation starting...**\n\n"
                    "Analysing source files and existing docs."
                ),
                ko=(
                    "**문서 생성 작업을 시작했습니다.**\n\n"
                    "소스 파일과 기존 문서를 분석하고 있습니다."
                ),
            ),
        )

        try:
            # a. Determine scope ------------------------------------------------
            scope_type, scope, target_files = await self._resolve_scope(
                ctx, ghes_client, owner, repo,
            )
            log.info(
                "scope.resolved",
                scope=scope,
                scope_type=scope_type,
                file_count=len(target_files),
            )

            # Resolve branch
            branch_name = await self._resolve_branch(
                ctx, ghes_client, owner, repo, config,
            )
            working_dir = os.getcwd()

            if ctx.pr_number or await git_branch_exists_remote(branch_name, cwd=working_dir):
                await git_checkout_existing_branch(branch_name, cwd=working_dir)
            else:
                await git_checkout_new_branch(
                    branch_name, config.agent.default_branch, cwd=working_dir,
                )

            # b. Collect source files and existing documentation ----------------
            changed_files = await self._read_files(
                target_files, ghes_client, owner, repo,
            )
            existing_docs = await self._collect_existing_docs(
                ghes_client, owner, repo,
            )

            # c. Send DOC_GEN_PROMPT to Copilot ---------------------------------
            prompt = self._prompt_mgr.render_prompt(
                "doc_gen",
                scope_type=scope_type,
                scope_label=scope,
                target_files=target_files,
                changed_files=changed_files,
                existing_docs=existing_docs,
            )

            head_before = await git_rev_parse("HEAD", cwd=working_dir)

            async with CopilotSessionManager(
                model=config.copilot.coder_model,
                timeout=config.agent.timeout_minutes * 60,
                working_dir=working_dir,
            ) as session:
                response = await session.execute(prompt)
                log.info("copilot.docs_generated", length=len(response))

            # e. Commit ---------------------------------------------------------
            await git_add_all(cwd=working_dir)
            staged_diff = await git_diff(staged=True, cwd=working_dir)
            head_after = await git_rev_parse("HEAD", cwd=working_dir)
            if staged_diff.strip():
                await git_commit(
                    f"docs: update documentation for {scope}",
                    cwd=working_dir,
                )
            elif head_after == head_before:
                msg = localized_text(
                    output_language,
                    en="Documentation was analysed, but no file changes were generated.",
                    ko="문서를 분석했지만 파일 변경은 생성되지 않았습니다.",
                )
                await self._post_comment(ghes_client, owner, repo, comment_target, msg)
                return msg

            # f. Push and create PR ---------------------------------------------
            await git_push(branch_name, cwd=working_dir)
            log.info("doc_gen.pushed", branch=branch_name)

            # If this was triggered from an issue (not an existing PR), create a PR
            pr_url = ""
            if not ctx.pr_number:
                display_scope = self._display_scope(scope, output_language)
                pr = await ghes_client.create_pull_request(
                    owner=owner,
                    repo=repo,
                    title=f"docs: update documentation for {scope}",
                    body=localized_text(
                        output_language,
                        en=(
                            "## Documentation Update\n\n"
                            f"Auto-generated documentation updates for: **{display_scope}**\n\n"
                            "### Scope\n"
                            "- Repository-wide documentation pass\n"
                            f"- {len(target_files)} seed source file(s) provided\n"
                            f"- {len(existing_docs)} existing doc(s) seeded\n\n"
                            "---\n*Generated by GHES Coding Agent*"
                        ),
                        ko=(
                            "## 문서 업데이트\n\n"
                            f"**{display_scope}** 범위의 문서 업데이트를 자동 생성했습니다.\n\n"
                            "### 범위\n"
                            "- 전체 리포지토리 문서 점검\n"
                            f"- seed로 제공한 소스 파일: {len(target_files)}개\n"
                            f"- seed로 제공한 기존 문서: {len(existing_docs)}개\n\n"
                            "---\n*GHES Coding Agent가 생성했습니다*"
                        ),
                    ),
                    head=branch_name,
                    base=config.agent.default_branch,
                )
                pr_url = pr.get("html_url", "")
                log.info("pr.created", pr_url=pr_url)

            # g. Post summary ---------------------------------------------------
            doc_summary = self._build_summary(
                scope, target_files, existing_docs, pr_url, output_language,
            )
            await self._post_comment(
                ghes_client, owner, repo, comment_target, doc_summary,
            )

            log.info("doc_gen_agent.completed")
            return doc_summary

        except Exception as exc:
            log.error("doc_gen_agent.failed", error=str(exc), exc_info=True)
            await self._post_comment(
                ghes_client, owner, repo, comment_target,
                localized_text(
                    output_language,
                    en=f"Documentation generation failed:\n\n```\n{exc}\n```",
                    ko=f"문서 생성 작업이 실패했습니다:\n\n```\n{exc}\n```",
                ),
            )
            raise

    # ------------------------------------------------------------------
    # Scope resolution
    # ------------------------------------------------------------------

    async def _resolve_scope(
        self,
        ctx: TriggerContext,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
    ) -> tuple[str, str, list[str]]:
        """Determine documentation scope and target files.

        Returns ``(scope_type, scope_label, list_of_file_paths)``.
        """
        # PR-based: document the changed files
        if ctx.pr_number:
            changed = await ghes_client.get_pull_request_files(
                owner, repo, ctx.pr_number,
            )
            files = [
                f["filename"]
                for f in changed
                if f.get("status") != "removed"
            ]
            return "pull_request", f"PR #{ctx.pr_number}", files

        # Full repo documentation
        return "repository", "full repository", []

    async def _resolve_branch(
        self,
        ctx: TriggerContext,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        config: AppConfig,
    ) -> str:
        """Resolve the branch to push documentation to."""
        if ctx.pr_number:
            pr_data = await ghes_client.get_pull_request(owner, repo, ctx.pr_number)
            return pr_data["head"]["ref"]

        return f"{config.agent.branch_prefix}docs-{ctx.issue_number or 'update'}"

    # ------------------------------------------------------------------
    # File collection
    # ------------------------------------------------------------------

    async def _read_files(
        self,
        target_files: list[str],
        ghes_client: GHESClient,
        owner: str,
        repo: str,
    ) -> list[dict]:
        """Read contents of target source files."""
        result: list[dict] = []
        for path in target_files[:20]:
            try:
                content = await ghes_client.get_file_content(owner, repo, path)
                ext = path.rsplit(".", 1)[-1] if "." in path else ""
                result.append({
                    "path": path,
                    "language": ext,
                    "content": content[:8000],
                })
            except Exception:
                self._log.debug("source_file.unavailable", path=path)
        return result

    async def _collect_existing_docs(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
    ) -> list[dict]:
        """Collect existing documentation files from the repository."""
        docs: list[dict] = []
        for doc_path in _DOC_FILES:
            try:
                content = await ghes_client.get_file_content(owner, repo, doc_path)
                docs.append({
                    "path": doc_path,
                    "content": content[:10000],
                })
            except Exception:
                # File doesn't exist — skip
                pass
        return docs

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _display_scope(scope: str, output_language: str) -> str:
        """Return a user-facing scope label without changing identifiers."""
        if scope == "full repository" and output_language == "ko":
            return "전체 리포지토리"
        return scope

    @staticmethod
    def _build_summary(
        scope: str,
        target_files: list[str],
        existing_docs: list[dict],
        pr_url: str,
        output_language: str,
    ) -> str:
        """Build a markdown summary of the documentation changes."""
        display_scope = DocGenAgent._display_scope(scope, output_language)
        if output_language == "ko":
            lines = [
                "**문서 생성 작업이 완료되었습니다.**\n",
                f"- **범위**: {display_scope}",
                f"- **seed로 제공한 소스 파일**: {len(target_files)}개",
                f"- **seed로 제공한 기존 문서**: {len(existing_docs)}개",
            ]
        else:
            lines = [
                "**Documentation generation complete**\n",
                f"- **Scope**: {display_scope}",
                f"- **Seed source files provided**: {len(target_files)}",
                f"- **Existing docs seeded**: {len(existing_docs)}",
            ]
        if pr_url:
            lines.append(
                localized_text(
                    output_language,
                    en=f"- **Pull request**: {pr_url}",
                    ko=f"- **Pull request**: {pr_url}",
                )
            )

        lines.append(
            localized_text(
                output_language,
                en="\n*Please review the generated documentation for accuracy.*",
                ko="\n*생성된 문서의 정확성을 확인해 주세요.*",
            )
        )
        return "\n".join(lines)

    async def _post_comment(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        number: int,
        body: str,
    ) -> None:
        """Post a comment, logging but not raising on failure."""
        try:
            await ghes_client.create_issue_comment(owner, repo, number, body)
            self._log.info("comment.posted", target=number)
        except Exception:
            self._log.warning("comment.post_failed", target=number, exc_info=True)
