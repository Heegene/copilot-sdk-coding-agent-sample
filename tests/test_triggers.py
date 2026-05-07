"""Tests for label trigger parsing."""

from __future__ import annotations

from agent.triggers.label_trigger import AgentType, LabelTrigger

# ===================================================================
# Label triggers
# ===================================================================

class TestLabelTrigger:
    def setup_method(self):
        self.trigger = LabelTrigger()

    def test_label_trigger_copilot_label(self, sample_issue_event):
        ctx = self.trigger.parse(sample_issue_event)
        assert ctx is not None
        assert ctx.agent_type == AgentType.CODER
        assert ctx.issue_number == 42
        assert ctx.owner == "acme"
        assert ctx.repo == "webapp"

    def test_label_trigger_review_label(self, sample_issue_event):
        sample_issue_event["label"]["name"] = "copilot-review"
        ctx = self.trigger.parse(sample_issue_event)
        assert ctx is not None
        assert ctx.agent_type == AgentType.REVIEWER

    def test_label_trigger_unknown_label(self, sample_issue_event):
        sample_issue_event["label"]["name"] = "bug"
        ctx = self.trigger.parse(sample_issue_event)
        assert ctx is None

    def test_label_trigger_pr_labeled(self, sample_pr_event):
        ctx = self.trigger.parse(sample_pr_event)
        assert ctx is not None
        assert ctx.agent_type == AgentType.REVIEWER
        assert ctx.pr_number == 99
        assert ctx.event_type == "pull_request"
        assert ctx.issue_number is None
