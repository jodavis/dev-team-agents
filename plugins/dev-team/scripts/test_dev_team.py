"""Tests for core step-machine components of dev_team.py.

Covers:
- exit_with_action() — JSON serialisation and exit-code 0
- compute_context_path() — base path resolution with/without DEV_TEAM_STATE_DIR
- Counter increment/reset logic — signoff_cycle_count, consecutive_failures,
  review_cycle_count
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_exit_with_action(descriptor: dict) -> subprocess.CompletedProcess:
    """Invoke exit_with_action in a child process to isolate sys.exit."""
    descriptor_json = json.dumps(descriptor)
    script = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS_DIR)!r}); "
        f"import json; from dev_team import exit_with_action; "
        f"exit_with_action(json.loads({descriptor_json!r}))"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# exit_with_action
# ---------------------------------------------------------------------------

class TestExitWithAction:
    def test_exits_with_code_0(self):
        result = _run_exit_with_action({"action": "done", "result": "success"})
        assert result.returncode == 0

    def test_emits_json_on_stdout(self):
        descriptor = {"action": "done", "result": "success", "reason": "all clean"}
        result = _run_exit_with_action(descriptor)
        parsed = json.loads(result.stdout.strip())
        assert parsed == descriptor

    def test_serializes_nested_list_fields(self):
        descriptor = {
            "action": "spawn_agent",
            "agent": "developer",
            "skill": "developer-implement",
            "context_file": "/home/.dev-team/repo/ADR-123.md",
            "read_sections": ["Researcher Brief", "Review Notes"],
            "write_section": "Implementation Summary",
            "result_format": "implemented | failed | needs_clarification",
        }
        result = _run_exit_with_action(descriptor)
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == descriptor

    def test_nothing_on_stderr(self):
        result = _run_exit_with_action({"action": "done", "result": "success"})
        assert result.stderr == ""

    def test_empty_descriptor_is_valid(self):
        result = _run_exit_with_action({})
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == {}


# ---------------------------------------------------------------------------
# compute_context_path
# ---------------------------------------------------------------------------

class TestComputeContextPath:
    def test_uses_home_dev_team_by_default(self, monkeypatch):
        monkeypatch.delenv("DEV_TEAM_STATE_DIR", raising=False)
        from dev_team import compute_context_path
        path = compute_context_path("ADR-123", "jodavis/AdaptiveRemote")
        expected = Path.home() / ".dev-team" / "jodavis" / "AdaptiveRemote" / "ADR-123.md"
        assert path == expected

    def test_uses_dev_team_state_dir_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEV_TEAM_STATE_DIR", str(tmp_path))
        from dev_team import compute_context_path
        path = compute_context_path("ADR-456", "myorg/myrepo")
        expected = tmp_path / "myorg" / "myrepo" / "ADR-456.md"
        assert path == expected

    def test_work_item_id_becomes_filename(self, monkeypatch):
        monkeypatch.delenv("DEV_TEAM_STATE_DIR", raising=False)
        from dev_team import compute_context_path
        path = compute_context_path("Issue-42", "org/repo")
        assert path.name == "Issue-42.md"

    def test_repo_slug_becomes_subdirectory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEV_TEAM_STATE_DIR", str(tmp_path))
        from dev_team import compute_context_path
        path = compute_context_path("ADR-1", "my-org/my-repo")
        assert path.parent == tmp_path / "my-org" / "my-repo"

    def test_hyphenated_repo_slug(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEV_TEAM_STATE_DIR", str(tmp_path))
        from dev_team import compute_context_path
        path = compute_context_path("ADR-99", "acme-corp/cool-service")
        assert path == tmp_path / "acme-corp" / "cool-service" / "ADR-99.md"


# ---------------------------------------------------------------------------
# signoff_cycle_count counter
# ---------------------------------------------------------------------------

class TestSignoffCycleCount:
    def make_sut(self, **kwargs):
        from dev_team import PipelineContext
        return PipelineContext(work_item_id="ADR-TEST", **kwargs)

    def test_starts_at_zero(self):
        ctx = self.make_sut()
        assert ctx.signoff_cycle_count == 0

    def test_increments_on_signoff_changes_requested(self):
        from dev_team import _apply_counter_updates
        ctx = self.make_sut()
        _apply_counter_updates(ctx, "signoff", "changes_requested")
        assert ctx.signoff_cycle_count == 1

    def test_accumulates_across_multiple_cycles(self):
        from dev_team import _apply_counter_updates
        ctx = self.make_sut()
        _apply_counter_updates(ctx, "signoff", "changes_requested")
        _apply_counter_updates(ctx, "signoff", "changes_requested")
        assert ctx.signoff_cycle_count == 2

    def test_resets_to_zero_on_signoff_approved(self):
        from dev_team import _apply_counter_updates
        ctx = self.make_sut(signoff_cycle_count=3)
        _apply_counter_updates(ctx, "signoff", "approved")
        assert ctx.signoff_cycle_count == 0

    def test_not_affected_by_reviewing_step(self):
        from dev_team import _apply_counter_updates
        ctx = self.make_sut(signoff_cycle_count=1)
        _apply_counter_updates(ctx, "reviewing", "changes_requested")
        assert ctx.signoff_cycle_count == 1

    def test_roundtrip_through_save_load(self, tmp_path):
        from dev_team import PipelineContext
        ctx = PipelineContext(work_item_id="ADR-123", signoff_cycle_count=2)
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.signoff_cycle_count == 2


# ---------------------------------------------------------------------------
# review_cycle_count counter
# ---------------------------------------------------------------------------

class TestReviewCycleCount:
    def make_sut(self, **kwargs):
        from dev_team import PipelineContext
        return PipelineContext(work_item_id="ADR-TEST", **kwargs)

    def test_starts_at_zero(self):
        ctx = self.make_sut()
        assert ctx.review_cycle_count == 0

    def test_increments_on_reviewing_step_changes_requested(self):
        from dev_team import _apply_counter_updates
        ctx = self.make_sut()
        _apply_counter_updates(ctx, "reviewing", "changes_requested")
        assert ctx.review_cycle_count == 1

    def test_increments_on_reviewing_step_approved(self):
        from dev_team import _apply_counter_updates
        ctx = self.make_sut()
        _apply_counter_updates(ctx, "reviewing", "approved")
        assert ctx.review_cycle_count == 1

    def test_resets_to_zero_on_signoff_approved(self):
        from dev_team import _apply_counter_updates
        ctx = self.make_sut(review_cycle_count=3)
        _apply_counter_updates(ctx, "signoff", "approved")
        assert ctx.review_cycle_count == 0

    def test_not_affected_by_signoff_changes_requested(self):
        from dev_team import _apply_counter_updates
        ctx = self.make_sut(review_cycle_count=1)
        _apply_counter_updates(ctx, "signoff", "changes_requested")
        assert ctx.review_cycle_count == 1

    def test_roundtrip_through_save_load(self, tmp_path):
        from dev_team import PipelineContext
        ctx = PipelineContext(work_item_id="ADR-123", review_cycle_count=3)
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.review_cycle_count == 3


# ---------------------------------------------------------------------------
# consecutive_failures counter
# ---------------------------------------------------------------------------

class TestConsecutiveFailures:
    def make_sut(self, **kwargs):
        from dev_team import PipelineContext
        return PipelineContext(work_item_id="ADR-TEST", **kwargs)

    def test_starts_at_zero(self):
        ctx = self.make_sut()
        assert ctx.consecutive_failures == 0

    def test_increments_on_agent_failure(self):
        from dev_team import _handle_agent_failure
        ctx = self.make_sut()
        _handle_agent_failure(ctx)
        assert ctx.consecutive_failures == 1

    def test_accumulates_across_multiple_failures(self):
        from dev_team import _handle_agent_failure
        ctx = self.make_sut()
        _handle_agent_failure(ctx)
        _handle_agent_failure(ctx)
        _handle_agent_failure(ctx)
        assert ctx.consecutive_failures == 3

    def test_resets_to_zero_on_agent_success(self):
        from dev_team import _handle_agent_success
        ctx = self.make_sut(consecutive_failures=5)
        _handle_agent_success(ctx)
        assert ctx.consecutive_failures == 0

    def test_reset_does_not_require_prior_failure(self):
        from dev_team import _handle_agent_success
        ctx = self.make_sut(consecutive_failures=0)
        _handle_agent_success(ctx)
        assert ctx.consecutive_failures == 0

    def test_roundtrip_through_save_load(self, tmp_path):
        from dev_team import PipelineContext
        ctx = PipelineContext(work_item_id="ADR-123", consecutive_failures=2)
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.consecutive_failures == 2


# ---------------------------------------------------------------------------
# troubleshooter_input field
# ---------------------------------------------------------------------------

class TestTroubleshooterInput:
    def test_defaults_to_empty_string(self):
        from dev_team import PipelineContext
        ctx = PipelineContext(work_item_id="ADR-TEST")
        assert ctx.troubleshooter_input == ""

    def test_roundtrip_through_save_load(self, tmp_path):
        from dev_team import PipelineContext
        ctx = PipelineContext(
            work_item_id="ADR-123",
            troubleshooter_input="Override the reviewer on thread abc",
        )
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.troubleshooter_input == "Override the reviewer on thread abc"

    def test_empty_string_roundtrip(self, tmp_path):
        from dev_team import PipelineContext
        ctx = PipelineContext(work_item_id="ADR-123", troubleshooter_input="")
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.troubleshooter_input == ""


# ---------------------------------------------------------------------------
# _researcher_validated
# ---------------------------------------------------------------------------

class TestResearcherValidated:
    def test_validated_indicator_returns_true(self):
        from dev_team import _researcher_validated
        assert _researcher_validated("validated") is True

    def test_failed_indicator_returns_false(self):
        from dev_team import _researcher_validated
        assert _researcher_validated("failed") is False

    def test_validated_with_whitespace_returns_true(self):
        from dev_team import _researcher_validated
        assert _researcher_validated("  validated\n") is True

    def test_failed_with_whitespace_returns_false(self):
        from dev_team import _researcher_validated
        assert _researcher_validated("  failed\n") is False

    def test_json_array_all_pass_returns_true(self):
        from dev_team import _researcher_validated
        content = '[{"status": "pass", "criterion": "Tests pass"}]'
        assert _researcher_validated(content) is True

    def test_json_array_with_fail_returns_false(self):
        from dev_team import _researcher_validated
        content = '[{"status": "fail", "criterion": "Tests pass"}]'
        assert _researcher_validated(content) is False

    def test_json_array_with_partial_returns_false(self):
        from dev_team import _researcher_validated
        content = '[{"status": "partial", "criterion": "Tests pass"}]'
        assert _researcher_validated(content) is False

    def test_json_array_in_fenced_block_with_fail(self):
        from dev_team import _researcher_validated
        content = '```json\n[{"status": "fail", "criterion": "x"}]\n```'
        assert _researcher_validated(content) is False

    def test_description_containing_never_failed_is_not_false_positive(self):
        from dev_team import _researcher_validated
        # "failed" substring in a description must not cause a false-positive
        content = '[{"status": "pass", "criterion": "Tests never failed"}]'
        assert _researcher_validated(content) is True

    def test_unrecognised_content_returns_false(self):
        from dev_team import _researcher_validated
        assert _researcher_validated("some unexpected output") is False

    def test_empty_string_returns_false(self):
        from dev_team import _researcher_validated
        assert _researcher_validated("") is False
