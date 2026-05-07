"""Multi-model code reviewer agent.

Runs Claude and GPT-5.4 models in parallel under the same PR context and
shared review rubric, posts individual reviews, then posts a consolidated
summary with inline GitHub Suggested Changes.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog

from agent.config import AppConfig
from agent.copilot_session import CopilotSessionManager
from agent.ghes_client import GHESClient
from agent.triggers.label_trigger import TriggerContext
from agent.utils.prompts import (
    PromptManager,
    localized_text,
)
from agent.utils.suggestions import (
    format_suggestion,
    parse_suggestion_response,
)

logger = structlog.get_logger(__name__)

# Maximum characters for the diff to avoid exceeding token limits.
MAX_DIFF_CHARS = 80_000
MAX_FILE_CONTENT_CHARS = 20_000

# Marker for review context chaining across runs. The stored body must remain
# inside one HTML comment so GitHub does not render it as a duplicate report.
_REVIEW_CONTEXT_MARKER = "<!-- review-context-v{version}"
_REVIEW_CONTEXT_PATTERN = re.compile(
    r"<!-- review-context-v(\d+)(?:\s*-->)?\n(.*?)(?:<!--\s*)?/review-context\s*-->",
    re.DOTALL,
)


class ReviewerAgent:
    """Runs parallel multi-model code reviews on a pull request.

    Uses two models (default: Claude and GPT-5.4) concurrently under the same
    review conditions. Each model reviews the full PR with a model-specific
    emphasis, then a summary pass deduplicates findings and calls out
    agreement or disagreement.
    """

    def __init__(self) -> None:
        self._log = logger.bind(agent="reviewer")
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
        """Run multi-model review on the PR described by *ctx*."""
        owner, repo = ctx.owner, ctx.repo
        pr_number = ctx.pr_number
        if pr_number is None:
            raise ValueError("ReviewerAgent requires a pull request context")

        self._log = self._log.bind(owner=owner, repo=repo, pr=pr_number)
        self._log.info("review_starting")

        self._prompt_mgr.set_output_language(config.agent.output_language)
        output_language = config.agent.output_language

        # 1. Load previous review context (if any)
        previous_context = await self._load_review_context(
            ghes_client, owner, repo, pr_number,
        )
        if previous_context:
            self._log.info("loaded_previous_context", version=previous_context["version"])

        # 2. Post starting comment
        await ghes_client.create_pr_comment(
            owner, repo, pr_number,
            localized_text(
                output_language,
                en=(
                    "**Multi-model code review starting...**\n\n"
                    "Two AI models are analysing this PR in parallel with the same "
                    "PR anchors and review rubric. They can inspect related files "
                    "in the checked-out repository when needed. Individual reviews "
                    "will be posted shortly."
                ),
                ko=(
                    "**멀티 모델 코드 리뷰를 시작했습니다.**\n\n"
                    "두 AI 모델이 같은 PR anchor와 리뷰 기준으로 이 PR을 병렬 "
                    "분석하고 있습니다. 필요하면 체크아웃된 리포지토리의 관련 "
                    "파일도 확인할 수 있습니다. 각 모델의 리뷰가 곧 게시됩니다."
                ),
            ),
        )

        # 3. Collect PR context
        pr_context = await self._collect_pr_context(
            ghes_client, owner, repo, pr_number,
        )

        # 4. Prepare rendered prompts
        claude_prompt = self._prompt_mgr.render_prompt(
            "reviewer_claude",
            diff=pr_context["diff"],
            file_list=pr_context["file_list"],
        )
        codex_prompt = self._prompt_mgr.render_prompt(
            "reviewer_gpt",
            diff=pr_context["diff"],
            file_list=pr_context["file_list"],
        )

        # Append file contents as extra context when available
        file_context_block = self._build_file_context_block(
            pr_context["file_contents"],
        )
        if file_context_block:
            claude_prompt += f"\n\n## File Contents\n{file_context_block}"
            codex_prompt += f"\n\n## File Contents\n{file_context_block}"

        # Append previous review context if available
        if previous_context:
            context_note = (
                f"\n\n## Previous Review Context (v{previous_context['version']})\n"
                f"{previous_context['summary']}\n\n"
                "Focus on new or unresolved issues. Avoid repeating already-acknowledged findings."
            )
            claude_prompt += context_note
            codex_prompt += context_note

        # 5. Run parallel reviews
        models = list(config.copilot.reviewer_models)
        claude_model = models[0] if models else "claude-sonnet-4.5"
        codex_model = models[1] if len(models) > 1 else "gpt-4.1"

        self._log.info(
            "running_parallel_reviews",
            claude_model=claude_model,
            gpt_model=codex_model,
        )

        async def _labeled_review(
            label: str, model: str, prompt: str,
        ) -> tuple[str, str, str | None, BaseException | None]:
            try:
                result = await self._run_model_review(model, prompt, config)
                return (label, model, result, None)
            except BaseException as exc:  # noqa: BLE001 — surface to caller
                return (label, model, None, exc)

        review_tasks = [
            asyncio.create_task(
                _labeled_review("Claude", claude_model, claude_prompt),
            ),
            asyncio.create_task(
                _labeled_review("GPT-5.4", codex_model, codex_prompt),
            ),
        ]

        # 5b. Post each individual review as a PR comment as soon as it
        #     completes (whichever model finishes first is posted first).
        claude_review: str = ""
        codex_review: str = ""
        claude_ok = False
        codex_ok = False

        for finished in asyncio.as_completed(review_tasks):
            label, model, result, err = await finished

            if err is not None:
                self._log.error(
                    "model_review_failed", label=label, model=model, error=str(err),
                )
                comment_body = localized_text(
                    output_language,
                    en=f"## {label} review failed (`{model}`)\n\n```\n{err}\n```",
                    ko=f"## {label} 리뷰 실패 (`{model}`)\n\n```\n{err}\n```",
                )
                review_text = localized_text(
                    output_language,
                    en=f"{label} review failed: {err}",
                    ko=f"{label} 리뷰 실패: {err}",
                )
            else:
                self._log.info("model_review_posted", label=label, model=model)
                comment_body = localized_text(
                    output_language,
                    en=f"## {label} review (`{model}`)\n\n{result}",
                    ko=f"## {label} 리뷰 (`{model}`)\n\n{result}",
                )
                review_text = result or ""

            try:
                await ghes_client.create_pr_comment(
                    owner, repo, pr_number, comment_body,
                )
            except Exception:
                self._log.warning(
                    "individual_review_post_failed", label=label, exc_info=True,
                )

            if label == "Claude":
                claude_review = review_text
                claude_ok = err is None
            else:
                codex_review = review_text
                codex_ok = err is None

        # 6. Generate consolidated summary (includes both reviews)
        if claude_ok or codex_ok:
            summary = await self._generate_summary(
                claude_review, codex_review, config,
            )
        else:
            summary = localized_text(
                output_language,
                en=(
                    "## AI Code Review Summary\n\n"
                    "Both models failed to produce a review. "
                    "Please re-trigger or review manually."
                ),
                ko=(
                    "## AI 코드 리뷰 요약\n\n"
                    "두 모델 모두 리뷰 생성에 실패했습니다. "
                    "다시 실행하거나 수동으로 리뷰해 주세요."
                ),
            )

        # 8. Post summary as PR review (COMMENT event)
        await ghes_client.create_pr_review(
            owner, repo, pr_number, body=summary, event="COMMENT",
        )

        # 9. Generate and post inline code suggestions
        if claude_ok or codex_ok:
            await self._generate_and_post_suggestions(
                ghes_client, owner, repo, pr_number,
                claude_review, codex_review,
                summary, pr_context, config,
            )

        # 10. Store review context for future runs
        await self._store_review_context(
            ghes_client, owner, repo, pr_number,
            summary, previous_context,
        )

        # 11. Update labels
        try:
            await ghes_client.update_issue_labels(
                owner, repo, pr_number,
                add_labels=["review-complete"],
            )
        except Exception:
            self._log.warning("failed_to_add_label", exc_info=True)

        self._log.info("review_complete")
        return localized_text(
            output_language,
            en="Code review is complete. Check the PR for review results and code suggestions.",
            ko="코드 리뷰가 완료되었습니다. PR에서 리뷰 결과와 코드 제안을 확인하세요.",
        )

    # ------------------------------------------------------------------
    # PR context collection
    # ------------------------------------------------------------------

    async def _collect_pr_context(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any]:
        """Gather diff, changed files, and file contents for the PR."""
        # Get PR details for head ref
        pr_detail = await ghes_client.get_pull_request(owner, repo, pr_number)
        head_ref = pr_detail.get("head", {}).get("ref", "main")

        diff_task = asyncio.create_task(
            ghes_client.get_pull_request_diff(owner, repo, pr_number),
        )
        files_task = asyncio.create_task(
            ghes_client.get_pull_request_files(owner, repo, pr_number),
        )

        diff, changed_files = await asyncio.gather(diff_task, files_task)

        # Sort files by number of changes (most-changed first) so we
        # prioritise them when truncating.
        changed_files.sort(
            key=lambda f: f.get("changes", 0), reverse=True,
        )
        file_list = [f["filename"] for f in changed_files]

        # Truncate diff if too large for the LLM prompt, but keep
        # the original for line-validation when posting suggestions.
        full_diff = diff
        if len(diff) > MAX_DIFF_CHARS:
            self._log.warning(
                "diff_truncated",
                original=len(diff),
                limit=MAX_DIFF_CHARS,
            )
            diff = diff[:MAX_DIFF_CHARS] + "\n\n… [diff truncated]"

        # Download contents for changed files (best-effort)
        file_contents: dict[str, str] = {}
        total_chars = 0
        for f in changed_files:
            if total_chars >= MAX_FILE_CONTENT_CHARS:
                break
            path = f["filename"]
            # Skip binary / deleted files
            if f.get("status") == "removed":
                continue
            try:
                content = await ghes_client.get_file_content(
                    owner, repo, path, ref=head_ref,
                )
                remaining = MAX_FILE_CONTENT_CHARS - total_chars
                if len(content) > remaining:
                    content = content[:remaining] + "\n… [file truncated]"
                file_contents[path] = content
                total_chars += len(content)
            except Exception:
                self._log.debug("file_content_unavailable", path=path)

        return {
            "diff": diff,
            "full_diff": full_diff,
            "file_list": file_list,
            "changed_files": changed_files,
            "file_contents": file_contents,
        }

    # ------------------------------------------------------------------
    # Model execution helpers
    # ------------------------------------------------------------------

    async def _run_model_review(
        self,
        model: str,
        prompt: str,
        config: AppConfig,
    ) -> str:
        """Run a single review through a Copilot session with *model*."""
        self._log.info("model_review_start", model=model)
        timeout = config.agent.timeout_minutes * 60

        async with CopilotSessionManager(
            model=model, timeout=timeout,
        ) as session:
            result = await session.execute(prompt)

        self._log.info("model_review_complete", model=model, length=len(result))
        return result

    async def _generate_summary(
        self,
        claude_review: str,
        codex_review: str,
        config: AppConfig,
    ) -> str:
        """Synthesise both reviews into a consolidated summary."""
        self._log.info("generating_summary")

        prompt = self._prompt_mgr.render_prompt(
            "reviewer_summary",
            claude_review=claude_review,
            gpt_review=codex_review,
        )

        summary_model = config.copilot.reviewer_summary_model
        timeout = config.agent.timeout_minutes * 60

        async with CopilotSessionManager(
            model=summary_model, timeout=timeout,
        ) as session:
            raw_summary = await session.execute(prompt)

        # Defense-in-depth: even with the prompt forbidding it, models
        # sometimes append a "Suggested Changes" / "Suggestions" / "Code
        # Fixes" section. Strip it so it doesn't show up next to the
        # dedicated inline-suggestion review.
        raw_summary = self._strip_suggestion_sections(raw_summary)

        # Force literal reviewer labels (Claude / GPT-5.4 / Both) — the
        # summary model frequently ignores the prompt and emits "A", "B",
        # "A, B" regardless.
        raw_summary = self._normalize_reviewer_labels(raw_summary)

        # Drop any row the model marked as "[truncated]" / "..." — the
        # prompt explicitly forbids table truncation, but models still do
        # it. Better to drop the placeholder row than to show it.
        raw_summary = self._drop_truncated_rows(raw_summary)

        # The raw summary already contains its own "### ✅ Final Verdict"
        # section, so we only prepend the top-level heading — no trailing
        # "Overall Assessment" wrapper (it was duplicating the verdict).
        heading = localized_text(
            config.agent.output_language,
            en="## AI Code Review Summary",
            ko="## AI 코드 리뷰 요약",
        )
        summary = f"{heading}\n\n{raw_summary}"

        self._log.info("summary_generated", length=len(summary))
        return summary

    @staticmethod
    def _normalize_reviewer_labels(text: str) -> str:
        """Rewrite single-letter reviewer labels in the consensus table.

        The summary model is instructed to use "Claude", "GPT-5.4", or
        "Both" in the Reviewer(s) column but frequently emits "A", "B",
        or "A, B" instead. This pass rewrites markdown table cells that
        contain only those short labels — including markdown-wrapped
        variants like ``**A**`` or `` `A` ``.
        """
        def _clean_token(token: str) -> str:
            # Strip whitespace and common markdown emphasis wrappers.
            t = token.strip()
            t = t.strip("*_`\"' ")
            return t.upper()

        def _map_cell(cell: str) -> str:
            raw = cell.strip().strip("*_`\"' ")
            tokens = {_clean_token(t) for t in raw.split(",") if t.strip()}
            if not tokens or not tokens.issubset({"A", "B"}):
                return cell
            if tokens == {"A"}:
                return " Claude "
            if tokens == {"B"}:
                return " GPT-5.4 "
            return " Both "

        out_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.lstrip()
            # Table body rows start with "|" and have >= 5 cells.
            if stripped.startswith("|") and stripped.count("|") >= 5:
                # Skip header/separator rows that contain "---".
                if "---" in line:
                    out_lines.append(line)
                    continue
                parts = line.split("|")
                # Last cell (reviewer column) is parts[-2]; parts[-1] is
                # the trailing empty segment after the final "|".
                if len(parts) >= 2:
                    parts[-2] = _map_cell(parts[-2])
                    line = "|".join(parts)
            out_lines.append(line)
        return "\n".join(out_lines)

    @staticmethod
    def _drop_truncated_rows(text: str) -> str:
        """Remove table rows that are placeholder truncation markers."""
        pattern = re.compile(
            r"\.\.\.\.?\s*\[truncated\]|\u2026\s*\[truncated\]",
            re.IGNORECASE,
        )
        out: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith("|")
                and stripped.count("|") >= 3
                and pattern.search(stripped)
                and "---" not in stripped
            ):
                continue
            out.append(line)
        return "\n".join(out)

    @staticmethod
    def _strip_suggestion_sections(text: str) -> str:
        """Truncate the summary at/after the Final Verdict section.

        The prompt instructs the model to stop after the Final Verdict,
        but models frequently keep going with a "Suggested Changes",
        "Code Fixes", or similar section that duplicates the dedicated
        inline-suggestion review. Strategy:

        1. Find the Final Verdict heading.
        2. Cut at the next same- or higher-level heading after it.
        3. As a fallback, also strip any heading that matches forbidden
           keywords, even if no verdict heading was found.
        """
        lines = text.splitlines()
        verdict_idx: int | None = None
        verdict_pat = re.compile(
            r"(?i)final\s+verdict|\u6700\u7d42\s*\u5224\u5b9a|"
            r"\ucd5c\uc885\s*(?:\ud310\ub2e8|\uacb0\ub860|\ud3c9\uac00)"
        )

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("#") and verdict_pat.search(stripped):
                verdict_idx = i
                break

        keyword_pat = re.compile(
            r"(?i)suggested\s+changes?|code\s+fix(?:es)?|suggestion\s+block|"
            r"\uc81c\uc548\s*\ubcc0\uacbd|\uc218\uc815\s*\uc81c\uc548|"
            r"\uc778\ub77c\uc778\s*\uc81c\uc548"
        )

        if verdict_idx is not None:
            # Cut at the next heading of any level that appears after the
            # verdict heading — models tend to start "Suggested Changes"
            # as a same-level section right after.
            for j in range(verdict_idx + 1, len(lines)):
                stripped = lines[j].lstrip()
                if stripped.startswith("#"):
                    return "\n".join(lines[:j]).rstrip()
            return text

        # No verdict heading found — keyword-based fallback.
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#") and keyword_pat.search(line):
                return "\n".join(lines[:i]).rstrip()
        return text

    # ------------------------------------------------------------------
    # Suggestion generation
    # ------------------------------------------------------------------

    async def _generate_and_post_suggestions(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        pr_number: int,
        claude_review: str,
        codex_review: str,
        consensus_summary: str,
        pr_context: dict[str, Any],
        config: AppConfig,
    ) -> None:
        """Generate inline code suggestions and post them as a PR review."""
        self._log.info("generating_suggestions")

        file_context_block = self._build_file_context_block(
            pr_context["file_contents"],
        )

        # Build the diff line map early so we can pass valid ranges
        # to the suggestion model as well.
        diff_line_map = self._parse_diff_line_map(
            pr_context.get("full_diff", pr_context.get("diff", "")),
        )

        valid_ranges_lines: list[str] = []
        for path in pr_context["file_list"]:
            lines = sorted(diff_line_map.get(path, []))
            if lines:
                ranges = self._compress_line_ranges(lines)
                valid_ranges_lines.append(f"- `{path}`: {ranges}")

        suggestion_prompt = self._prompt_mgr.render_prompt(
            "reviewer_suggestion",
            claude_review=claude_review,
            gpt_review=codex_review,
            consensus_summary=consensus_summary,
            changed_files_list="\n".join(
                f"- {p}" for p in pr_context["file_list"]
            ) or "(no files changed)",
            valid_line_ranges="\n".join(valid_ranges_lines) or "(unknown)",
            file_contents=file_context_block or "(no file contents available)",
            explanation_language_note=(
                "**LANGUAGE REQUIREMENT — READ CAREFULLY.** "
                "Write the content of every `### EXPLANATION:` section "
                "**in Korean (한국어)**. This is MANDATORY. Even though the "
                "section markers themselves stay in English, the explanation "
                "text that follows `### EXPLANATION:` MUST be Korean prose. "
                "Code identifiers, file paths, and the SEVERITY value remain "
                "English regardless."
                if config.agent.output_language == "ko"
                else "Write the content of every `### EXPLANATION:` section in English."
            ),
            output_language_directive="",
        )

        suggestion_model = config.copilot.reviewer_suggestion_model
        timeout = config.agent.timeout_minutes * 60

        try:
            async with CopilotSessionManager(
                model=suggestion_model, timeout=timeout,
            ) as session:
                raw_suggestions = await session.execute(suggestion_prompt)

            findings = parse_suggestion_response(raw_suggestions)
            self._log.info(
                "suggestions_parsed",
                total_findings=len(findings),
                file_paths=[f["file_path"] for f in findings],
            )

            if not findings:
                # Surface a small preview so we can tell whether the
                # model returned nothing vs. returned something we
                # failed to parse (e.g. translated section markers).
                preview = (raw_suggestions or "").strip()[:400]
                self._log.warning(
                    "no_actionable_suggestions",
                    raw_length=len(raw_suggestions or ""),
                    raw_preview=preview,
                )
                return

            # Post inline review comments with Apply suggestion buttons
            changed_files = {f["filename"] for f in pr_context["changed_files"]}

            # Build a per-file set of line numbers that are actually part
            # of the diff (added or context lines on the RIGHT side).
            # GitHub rejects inline comments whose line is outside the
            # diff hunks with HTTP 422.
            # diff_line_map was already built above for the prompt.

            inline_comments: list[dict] = []
            skipped_out_of_diff = 0
            skipped_unknown_path = 0

            for finding in findings:
                raw_path = finding.get("file_path", "")
                resolved = self._resolve_finding_path(raw_path, changed_files)
                if resolved is None:
                    skipped_unknown_path += 1
                    continue

                line_no = finding.get("line")
                valid_lines = diff_line_map.get(resolved)
                if valid_lines is None or line_no not in valid_lines:
                    # Try to snap to the nearest valid line in the same
                    # file (within 3 lines) — LLMs often drift by one.
                    snapped = self._snap_to_diff_line(
                        line_no, valid_lines or set(), max_delta=3,
                    )
                    if snapped is None:
                        skipped_out_of_diff += 1
                        continue
                    line_no = snapped

                comment = format_suggestion(
                    file_path=resolved,
                    start_line=line_no,
                    end_line=line_no,
                    original_code=finding["original"],
                    suggested_code=finding["suggested"],
                    explanation=finding["explanation"],
                )
                inline_comments.append(comment)

            if skipped_out_of_diff or skipped_unknown_path:
                self._log.info(
                    "suggestions_filtered",
                    kept=len(inline_comments),
                    skipped_out_of_diff=skipped_out_of_diff,
                    skipped_unknown_path=skipped_unknown_path,
                )

            if inline_comments:
                posted = await self._post_inline_comments_safely(
                    ghes_client, owner, repo, pr_number,
                    inline_comments, config.agent.output_language,
                )
                self._log.info("suggestions_posted", count=posted)
            else:
                self._log.info("no_valid_suggestions_for_changed_files")

        except Exception:
            self._log.warning("suggestion_generation_failed", exc_info=True)

    async def _post_inline_comments_safely(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        pr_number: int,
        inline_comments: list[dict],
        output_language: str,
    ) -> int:
        """Post inline comments, falling back to per-file retries on 422.

        GitHub rejects the entire review if any single comment is
        invalid. We first try a single review containing every comment;
        if that fails, we retry one file at a time; if a file still
        fails, we retry each comment individually so at least the valid
        ones get through.
        """
        body = self._inline_suggestion_review_body(output_language)

        # Retry attempts use an empty body to avoid duplicate headers
        # when the batch is split into multiple reviews.
        retry_body = ""

        # Attempt 1: everything in one review.
        try:
            await ghes_client.create_pr_review(
                owner, repo, pr_number,
                body=body, event="COMMENT", comments=inline_comments,
            )
            return len(inline_comments)
        except Exception as exc:
            self._log.warning(
                "inline_review_batch_failed",
                total=len(inline_comments),
                error=str(exc)[:200],
            )

        # Attempt 2: group by file and retry each group.
        by_file: dict[str, list[dict]] = {}
        for c in inline_comments:
            by_file.setdefault(c["path"], []).append(c)

        posted = 0
        for comments in by_file.values():
            try:
                await ghes_client.create_pr_review(
                    owner, repo, pr_number,
                    body=retry_body, event="COMMENT", comments=comments,
                )
                posted += len(comments)
                continue
            except Exception:
                pass

            # Attempt 3: per-comment retry for this file.
            for c in comments:
                try:
                    await ghes_client.create_pr_review(
                        owner, repo, pr_number,
                        body=retry_body, event="COMMENT", comments=[c],
                    )
                    posted += 1
                except Exception:
                    self._log.warning(
                        "inline_comment_rejected",
                        path=c.get("path"), line=c.get("line"),
                    )

        return posted

    @staticmethod
    def _inline_suggestion_review_body(output_language: str) -> str:
        """Build the review body that introduces inline suggestions."""
        return localized_text(
            output_language,
            en=(
                "## Inline Code Suggestions\n\n"
                "Use **Commit suggestion** to apply a suggestion directly."
            ),
            ko=(
                "## 인라인 코드 제안\n\n"
                "**Commit suggestion**을 사용해 제안 내용을 바로 적용할 수 있습니다."
            ),
        )

    @staticmethod
    def _parse_diff_line_map(diff_text: str) -> dict[str, set[int]]:
        """Return {file_path: {valid RIGHT-side line numbers}}.

        Parses unified diff hunks (``@@ -a,b +c,d @@``) and collects
        every line number on the new (RIGHT) side that is either added
        (``+``) or context (`` ``). These are the only lines GitHub
        accepts as targets for inline review comments.
        """
        result: dict[str, set[int]] = {}
        current_path: str | None = None
        new_line: int | None = None

        hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

        for line in diff_text.splitlines():
            if line.startswith("+++ "):
                # e.g. "+++ b/path/to/file.py" or "+++ /dev/null"
                header = line[4:].strip()
                if header == "/dev/null":
                    current_path = None
                else:
                    # Strip "b/" prefix if present.
                    current_path = header[2:] if header.startswith("b/") else header
                    result.setdefault(current_path, set())
                new_line = None
                continue

            if line.startswith("--- "):
                continue

            m = hunk_re.match(line)
            if m:
                new_line = int(m.group(1))
                continue

            if current_path is None or new_line is None:
                continue

            if (line.startswith("+") and not line.startswith("+++")) or line.startswith(" "):
                result[current_path].add(new_line)
                new_line += 1
            elif line.startswith("-"):
                # deletion — RIGHT side unchanged
                pass
            elif line.startswith("\\"):
                # "\ No newline at end of file"
                pass
            else:
                # empty line inside a hunk still counts as context.
                result[current_path].add(new_line)
                new_line += 1

        return result

    @staticmethod
    def _snap_to_diff_line(
        line: Any, valid_lines: set[int], *, max_delta: int,
    ) -> int | None:
        """Return the closest valid line to *line*, or None if too far."""
        if not isinstance(line, int) or not valid_lines:
            return None
        for delta in range(1, max_delta + 1):
            if (line + delta) in valid_lines:
                return line + delta
            if (line - delta) in valid_lines:
                return line - delta
        return None

    @staticmethod
    def _compress_line_ranges(lines: list[int]) -> str:
        """Compress a sorted list of line numbers into a readable range string.

        Example: [1, 2, 3, 5, 7, 8, 9] -> "L1-L3, L5, L7-L9"
        """
        if not lines:
            return ""
        ranges: list[str] = []
        start = prev = lines[0]
        for n in lines[1:]:
            if n == prev + 1:
                prev = n
            else:
                ranges.append(f"L{start}" if start == prev else f"L{start}-L{prev}")
                start = prev = n
        ranges.append(f"L{start}" if start == prev else f"L{start}-L{prev}")
        return ", ".join(ranges)

    @staticmethod
    def _resolve_finding_path(raw_path: str, changed_files: set[str]) -> str | None:
        """Best-effort mapping from an LLM-returned path to a real diff path.

        Handles common deviations: backticks/quotes, ``./`` or ``/`` prefixes,
        diff-style ``a/`` / ``b/`` prefixes, and basename fallbacks.
        Returns ``None`` if no unambiguous match is found.
        """
        if not raw_path or not changed_files:
            return None

        candidate = raw_path.strip().strip("`\"' ")
        if candidate in changed_files:
            return candidate

        # Strip common prefixes one by one.
        stripped = candidate
        for prefix in ("./", "/", "a/", "b/"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
        if stripped in changed_files:
            return stripped

        # Suffix match: LLM returned a fully-qualified path that ends
        # with one of the changed files.
        suffix_hits = [p for p in changed_files if candidate.endswith("/" + p)]
        if len(suffix_hits) == 1:
            return suffix_hits[0]

        # Basename match: LLM returned only the filename. Accept only
        # when unique across the diff.
        base = candidate.rsplit("/", 1)[-1]
        base_hits = [p for p in changed_files if p.rsplit("/", 1)[-1] == base]
        if len(base_hits) == 1:
            return base_hits[0]

        return None

    # ------------------------------------------------------------------
    # Review context chaining
    # ------------------------------------------------------------------

    async def _load_review_context(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any] | None:
        """Search PR comments for the latest review context marker."""
        try:
            comments = await ghes_client.get_issue_comments(
                owner, repo, pr_number,
            )
        except Exception:
            self._log.debug("could_not_load_comments", exc_info=True)
            return None

        latest: dict[str, Any] | None = None
        latest_version = 0

        for comment in comments:
            body = comment.get("body", "")
            match = _REVIEW_CONTEXT_PATTERN.search(body)
            if match:
                version = int(match.group(1))
                if version > latest_version:
                    latest_version = version
                    latest = {
                        "version": version,
                        "summary": match.group(2).strip(),
                    }

        return latest

    async def _find_existing_context_comment(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any] | None:
        """Find the existing review context comment on the PR, if any.

        Returns the full comment dict of the latest context comment,
        or None if none exists.
        """
        try:
            comments = await ghes_client.get_issue_comments(
                owner, repo, pr_number,
            )
        except Exception:
            self._log.debug("context_comment_search_failed", exc_info=True)
            return None

        latest_comment: dict[str, Any] | None = None
        latest_version = 0

        for comment in comments:
            body = comment.get("body", "")
            match = _REVIEW_CONTEXT_PATTERN.search(body)
            if match:
                version = int(match.group(1))
                if version >= latest_version:
                    latest_version = version
                    latest_comment = comment

        return latest_comment

    async def _store_review_context(
        self,
        ghes_client: GHESClient,
        owner: str,
        repo: str,
        pr_number: int,
        summary: str,
        previous_context: dict[str, Any] | None,
    ) -> None:
        """Store review context as a hidden comment, updating in-place if possible.

        Searches for an existing context comment and updates it via
        ``update_issue_comment`` to avoid accumulating duplicate comments.
        Falls back to creating a new comment when none exists yet.
        """
        version = (previous_context["version"] + 1) if previous_context else 1

        # Compress: take first 500 chars of summary as context
        compressed = summary[:500]
        if len(summary) > 500:
            compressed += "… [truncated]"

        marker_open = _REVIEW_CONTEXT_MARKER.format(version=version)
        body = (
            f"{marker_open}\n"
            f"{compressed}\n"
            "/review-context -->"
        )

        try:
            existing = await self._find_existing_context_comment(
                ghes_client, owner, repo, pr_number,
            )

            if existing and existing.get("id"):
                # Update the existing comment in-place
                await ghes_client.update_issue_comment(
                    owner, repo, existing["id"], body,
                )
                self._log.info(
                    "review_context_updated",
                    version=version,
                    comment_id=existing["id"],
                )
            else:
                # No prior context comment — create a new one
                await ghes_client.create_pr_comment(
                    owner, repo, pr_number, body,
                )
                self._log.info("review_context_stored", version=version)
        except Exception:
            self._log.warning("failed_to_store_context", exc_info=True)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_file_context_block(
        file_contents: dict[str, str],
    ) -> str:
        """Format downloaded file contents into a markdown block."""
        if not file_contents:
            return ""
        parts: list[str] = []
        for path, content in file_contents.items():
            parts.append(f"### `{path}`\n```\n{content}\n```")
        return "\n\n".join(parts)
