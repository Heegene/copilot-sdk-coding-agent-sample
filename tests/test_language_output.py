"""Tests for output-language rendering and localized agent-authored text."""

from __future__ import annotations

from unittest.mock import AsyncMock

from pytest import MonkeyPatch

from agent.agents.ci_fix_agent import CIFixAgent
from agent.agents.coder_agent import CoderAgent
from agent.agents.doc_gen_agent import DocGenAgent
from agent.agents.reviewer_agent import ReviewerAgent
from agent.config import AgentConfig, AppConfig
from agent.triggers.label_trigger import AgentType, TriggerContext
from agent.utils.prompts import PromptManager


def _make_issue_ctx() -> TriggerContext:
    """Build a minimal issue trigger context."""
    return TriggerContext(
        agent_type=AgentType.CODER,
        event_type="issues",
        owner="acme",
        repo="webapp",
        issue_number=42,
        pr_number=None,
        issue_title="Add checkout endpoint",
        issue_body="Create an endpoint for checkout.",
        creator="dev",
        server_url="https://github.example.com",
        run_id=None,
    )


class TestPromptLanguage:
    def test_prompt_manager_renders_english_by_default(self) -> None:
        """Default prompts ask for English explanatory output."""
        prompt = PromptManager().render_prompt(
            "coder_implement",
            issue_title="Test",
            issue_body="Body",
            repo_context="Existing repo",
            file_list=["src/app.py"],
        )

        assert "Write all explanations" in prompt
        assert "file paths" in prompt
        assert "`src/app.py`" in prompt

    def test_prompt_manager_renders_korean_directive(self) -> None:
        """Korean prompts request Korean prose while preserving file paths."""
        manager = PromptManager()
        manager.set_output_language("ko")

        prompt = manager.render_prompt(
            "coder_implement",
            issue_title="Test",
            issue_body="Body",
            repo_context="Existing repo",
            file_list=["src/app.py"],
        )

        assert "반드시 한국어" in prompt
        assert "파일 경로" in prompt
        assert "`src/app.py`" in prompt

    def test_reviewer_prompts_use_same_condition_full_scope(self) -> None:
        """Both reviewer prompts use the same full-scope rubric with only emphasis bias."""
        manager = PromptManager()
        claude_prompt = manager.render_prompt(
            "reviewer_claude",
            diff="diff --git a/src/app.py b/src/app.py",
            file_list=["src/app.py"],
        )
        gpt_prompt = manager.render_prompt(
            "reviewer_gpt",
            diff="diff --git a/src/app.py b/src/app.py",
            file_list=["src/app.py"],
        )

        for prompt in (claude_prompt, gpt_prompt):
            assert "same-condition, independent multi-model PR review" in prompt
            assert "Review the full change" in prompt
            assert "do not restrict yourself to only your emphasis areas" in prompt
            assert "The checked-out repository workspace is available" in prompt
            assert "changed files as review anchors" in prompt
            assert "not as the full review boundary" in prompt
            assert "use workspace tools to verify" in prompt
            assert "unchanged impacted files" in prompt
            assert "can only target valid PR" in prompt
            assert "## Shared Review Criteria" in prompt
            assert "**Security**" in prompt
            assert "**Architecture**" in prompt
            assert "**Maintainability**" in prompt
            assert "**Correctness**" in prompt
            assert "**Performance**" in prompt
            assert "**Edge Cases**" in prompt
            assert "**Error Handling**" in prompt
            assert "**Dependencies**" in prompt
            assert "**Confidence**: High | Medium | Low" in prompt

        assert "security, architecture, and maintainability" in claude_prompt
        assert "correctness, performance, robustness, and edge cases" in gpt_prompt

    def test_reviewer_summary_prompt_requires_agreement_analysis(self) -> None:
        """The consensus prompt makes same-condition agreement analysis explicit."""
        prompt = PromptManager().render_prompt(
            "reviewer_summary",
            claude_review="### Security: Missing validation",
            gpt_review="### Correctness: Missing validation",
        )

        assert "same-condition, independent code" in prompt
        assert "their model-specific emphasis areas were only weighting" in prompt
        assert "Mark findings raised by both reviewers as **Both**" in prompt
        assert "Do not create brand-new findings" in prompt
        assert "traceable to at least" in prompt
        assert "### Agreement / Disagreement Analysis" in prompt
        assert "`Claude`" in prompt
        assert "`GPT-5.4`" in prompt
        assert "`Both`" in prompt

    def test_reviewer_suggestion_prompt_uses_consensus_summary(self) -> None:
        """Inline suggestion generation is grounded in the consensus summary."""
        prompt = PromptManager().render_prompt(
            "reviewer_suggestion",
            claude_review="Claude finding",
            gpt_review="GPT finding",
            consensus_summary="## AI Code Review Summary\n\nBoth reviewers agree.",
            changed_files_list="- src/app.py",
            valid_line_ranges="- `src/app.py`: L10-L12",
            file_contents="### `src/app.py`\n```\nprint('hello')\n```",
            explanation_language_note="Write explanations in English.",
        )

        assert "## Consensus Summary (Allowed Finding Set):" in prompt
        assert "Both reviewers agree" in prompt
        assert "The Consensus Summary is the allowed finding set" in prompt
        assert "Do **not** invent new" in prompt
        assert "Only convert actionable accepted findings" in prompt
        assert "unchanged impacted files" in prompt
        assert "Valid Diff Line Ranges" in prompt

    def test_coder_pr_summary_prompt_uses_ground_truth_metadata(self) -> None:
        """PR summary prompt stays small and excludes verification claims."""
        prompt = PromptManager().render_prompt(
            "coder_pr_summary",
            issue_title="Build movie review app",
            issue_body_excerpt="Create routes and a review feed.",
            commit_subjects="feat: build movie review app",
            diff_stat="34 files changed, 1000 insertions(+)",
            name_status_summary="A\tsrc/App.jsx\nA\tpackage.json",
        )

        assert "ground-truth metadata" in prompt
        assert "Do not inspect files" in prompt
        assert "Do not list every changed file" in prompt
        assert "Do not mention verification" in prompt
        assert "### SUMMARY" in prompt
        assert "### VERIFICATION" not in prompt


class TestAgentAuthoredLanguage:
    async def test_coder_pr_body_uses_korean_without_translating_paths(
        self, monkeypatch: MonkeyPatch,
    ) -> None:
        """Coder PR body localizes prose without dumping the full file list."""
        import agent.agents.coder_agent as module

        async def fake_run_git(*args: str, cwd: str) -> str:
            if args[:2] == ("diff", "--name-status"):
                return "M\tsrc/app.py\nA\tdocs/checkout.md\n"
            if args[:2] == ("log", "origin/main..HEAD"):
                return "feat: add checkout endpoint\n"
            return ""

        monkeypatch.setattr(module, "_run_git", fake_run_git)

        body = await CoderAgent()._build_pr_body(
            _make_issue_ctx(), ".", "main", "ko",
        )

        assert "Closes #42" in body
        assert "## 요약" in body
        assert "## 검증" not in body
        assert "## 변경 규모" in body
        assert "2개 파일을 변경했습니다." in body
        assert "Files changed" in body
        assert "`src/app.py`" not in body
        assert "`docs/checkout.md`" not in body
        assert "feat: add checkout endpoint" not in body
        assert "## Changes" not in body

    async def test_coder_pr_body_uses_english_by_default(
        self, monkeypatch: MonkeyPatch,
    ) -> None:
        """English PR body remains English."""
        import agent.agents.coder_agent as module

        async def fake_run_git(*args: str, cwd: str) -> str:
            if args[:2] == ("diff", "--name-status"):
                return "M\tsrc/app.py\n"
            if args[:2] == ("log", "origin/main..HEAD"):
                return "feat: add checkout endpoint\n"
            return ""

        monkeypatch.setattr(module, "_run_git", fake_run_git)

        body = await CoderAgent()._build_pr_body(
            _make_issue_ctx(), ".", "main", "en",
        )

        assert "## Summary" in body
        assert "## Verification" not in body
        assert "## Change Footprint" in body
        assert "Changed 1 file(s)." in body
        assert "Files changed" in body
        assert "`src/app.py`" not in body
        assert "## 요약" not in body

    def test_doc_summary_respects_output_language(self) -> None:
        """DocGen summary localizes prose without changing PR URLs."""
        ko_summary = DocGenAgent._build_summary(
            "full repository", ["src/app.py"], [{"path": "README.md"}],
            "https://github.example.com/acme/webapp/pull/1", "ko",
        )
        en_summary = DocGenAgent._build_summary(
            "full repository", ["src/app.py"], [{"path": "README.md"}],
            "https://github.example.com/acme/webapp/pull/1", "en",
        )

        assert "전체 리포지토리" in ko_summary
        assert "seed로 제공한 소스 파일" in ko_summary
        assert "https://github.example.com/acme/webapp/pull/1" in ko_summary
        assert "Seed source files provided" in en_summary
        assert "full repository" in en_summary

    def test_doc_prompt_treats_pr_files_as_workspace_anchors(self) -> None:
        """Doc prompt uses PR files as anchors without hiding workspace exploration."""
        prompt = PromptManager().render_prompt(
            "doc_gen",
            scope_type="pull_request",
            scope_label="PR #7",
            target_files=["src/app.py", "docs/checkout.md"],
            changed_files=[{
                "path": "src/app.py",
                "language": "py",
                "content": "def checkout() -> None:\n    pass\n",
            }],
            existing_docs=[{"path": "README.md", "content": "# Webapp"}],
        )

        assert "Scope Context" in prompt
        assert "`pull_request`" in prompt
        assert "PR changed-file anchors" in prompt
        assert "related source, tests, configuration, and documentation" in prompt
        assert "`src/app.py`" in prompt
        assert "`docs/checkout.md`" in prompt
        assert "`README.md`" in prompt
        assert "## 📁 Changed Files" not in prompt

    def test_doc_prompt_repository_scope_requests_workspace_inventory(self) -> None:
        """Full-repo doc prompt asks the SDK agent to inspect the workspace first."""
        manager = PromptManager()
        manager.set_output_language("ko")

        prompt = manager.render_prompt(
            "doc_gen",
            scope_type="repository",
            scope_label="full repository",
            target_files=[],
            changed_files=[],
            existing_docs=[{"path": "docs/SETUP.md", "content": "# Setup"}],
        )

        assert "반드시 한국어" in prompt
        assert "repository-wide documentation pass" in prompt
        assert "workspace inventory" in prompt
        assert "`glob`" in prompt
        assert "`grep`" in prompt
        assert "`read_file`" in prompt
        assert "No seed source file contents were provided" in prompt
        assert "`docs/SETUP.md`" in prompt

    def test_reviewer_inline_suggestion_wrapper_respects_language(self) -> None:
        """Reviewer inline suggestion wrapper is localized, but button text stays stable."""
        ko_body = ReviewerAgent._inline_suggestion_review_body("ko")
        en_body = ReviewerAgent._inline_suggestion_review_body("en")

        assert "인라인 코드 제안" in ko_body
        assert "Inline Code Suggestions" in en_body
        assert "Commit suggestion" in ko_body
        assert "Commit suggestion" in en_body

    def test_reviewer_strips_korean_post_verdict_suggestion_section(self) -> None:
        """Korean final-verdict headings still stop duplicate suggestion sections."""
        text = "### 최종 판단\n승인합니다.\n\n### 수정 제안\n불필요한 섹션"

        stripped = ReviewerAgent._strip_suggestion_sections(text)

        assert "승인합니다." in stripped
        assert "수정 제안" not in stripped

    async def test_ci_fix_no_pr_message_respects_output_language(self) -> None:
        """CI fix agent status text also follows the global output language."""
        ctx = TriggerContext(
            agent_type=AgentType.CI_FIX,
            event_type="workflow_run",
            owner="acme",
            repo="webapp",
            issue_number=None,
            pr_number=None,
            issue_title="CI Fix",
            issue_body="",
            creator="dev",
            server_url="https://github.example.com",
            run_id="123",
        )
        config = AppConfig(agent=AgentConfig(output_language="ko"))

        result = await CIFixAgent().execute(ctx, AsyncMock(), config)

        assert "건너뛰었습니다" in result
