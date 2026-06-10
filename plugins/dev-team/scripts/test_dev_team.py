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


# ---------------------------------------------------------------------------
# --print-context-path CLI flag
# ---------------------------------------------------------------------------

class TestPrintContextPath:
    def test_prints_path_and_exits_zero(self, tmp_path, monkeypatch):
        """--print-context-path should print the path to stdout and exit 0."""
        monkeypatch.setenv("DEV_TEAM_STATE_DIR", str(tmp_path))
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "dev_team.py"),
             "ADR-123", "--print-context-path", "myorg/myrepo"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        expected = str(tmp_path / "myorg" / "myrepo" / "ADR-123.md")
        assert result.stdout.strip() == expected

    def test_uses_state_dir_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEV_TEAM_STATE_DIR", str(tmp_path))
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "dev_team.py"),
             "Issue-99", "--print-context-path", "org/repo"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        assert "Issue-99.md" in result.stdout

    def test_nothing_on_stderr(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEV_TEAM_STATE_DIR", str(tmp_path))
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "dev_team.py"),
             "ADR-1", "--print-context-path", "a/b"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.stderr == ""


# ---------------------------------------------------------------------------
# exit_with_action supports "actions" list (parallel descriptor)
# ---------------------------------------------------------------------------

class TestExitWithActionParallel:
    def test_serializes_actions_list(self):
        descriptor = {
            "action": "spawn_agent",
            "message": "Running in parallel.",
            "actions": [
                {"agent": "reviewer", "skill": "reviewer-sign-off",
                 "context_file": "/tmp/ctx.md", "read_sections": [],
                 "write_section": "Signoff Review", "result_format": "approved | changes_requested"},
                {"agent": "researcher", "skill": "researcher-validate",
                 "context_file": "/tmp/ctx.md", "read_sections": ["Researcher Brief"],
                 "write_section": "Signoff Research", "result_format": "validated | failed"},
            ],
        }
        result = _run_exit_with_action(descriptor)
        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert parsed["action"] == "spawn_agent"
        assert len(parsed["actions"]) == 2
        assert parsed["actions"][0]["skill"] == "reviewer-sign-off"
        assert parsed["actions"][1]["skill"] == "researcher-validate"


# ---------------------------------------------------------------------------
# message field in exit_with_action descriptors
# ---------------------------------------------------------------------------

class TestExitWithActionMessage:
    def test_message_field_is_serialized(self):
        descriptor = {
            "action": "spawn_agent",
            "message": "Developer is implementing.",
            "agent": "developer",
            "skill": "developer-implement",
        }
        result = _run_exit_with_action(descriptor)
        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert parsed["message"] == "Developer is implementing."

    def test_descriptor_without_message_still_valid(self):
        """Backward-compat: descriptors without 'message' still work."""
        descriptor = {"action": "done", "result": "success"}
        result = _run_exit_with_action(descriptor)
        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert "message" not in parsed


# ---------------------------------------------------------------------------
# ReviewStep pr_url extraction from "PR URL" section
# ---------------------------------------------------------------------------

class TestReviewStepPrUrlExtraction:
    def make_sut(self, **kwargs):
        from dev_team import PipelineContext
        return PipelineContext(work_item_id="ADR-TEST", **kwargs)

    def test_pr_url_saved_to_frontmatter_after_extraction(self, tmp_path):
        """When pending_agent==create-pr and PR URL section is written, pr_url lands in frontmatter."""
        from dev_team import PipelineContext
        ctx = self.make_sut(
            state="reviewing",
            pending_agent="create-pr",
            work_summaries=["# Summary"],
        )
        context_path = tmp_path / "ctx.md"
        ctx.save(context_path)

        # Simulate task-runner writing the PR URL section
        text = context_path.read_text(encoding="utf-8")
        text += "\n<!-- section:PR URL -->\n\nhttps://github.com/org/repo/pull/42\n"
        context_path.write_text(text, encoding="utf-8")

        # load() should NOT pick up pr_url from section (fallback removed)
        loaded = PipelineContext.load(context_path)
        assert loaded.pr_url == ""

    def test_load_does_not_fallback_to_pr_url_section(self, tmp_path):
        """After removing the fallback, pr_url from section is NOT loaded automatically."""
        from dev_team import PipelineContext
        ctx = self.make_sut()
        path = tmp_path / "ctx.md"
        ctx.save(path)
        text = path.read_text(encoding="utf-8")
        text += "\n<!-- section:PR URL -->\n\nhttps://github.com/org/repo/pull/99\n"
        path.write_text(text, encoding="utf-8")
        loaded = PipelineContext.load(path)
        # Section fallback removed — pr_url should be empty
        assert loaded.pr_url == ""

    def test_pr_url_in_frontmatter_is_loaded(self, tmp_path):
        """pr_url set explicitly in frontmatter IS loaded correctly."""
        from dev_team import PipelineContext
        ctx = self.make_sut(pr_url="https://github.com/org/repo/pull/7")
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.pr_url == "https://github.com/org/repo/pull/7"


# ---------------------------------------------------------------------------
# _get_failing_pr_checks
# ---------------------------------------------------------------------------

class TestGetFailingPrChecks:
    def test_returns_empty_when_gh_not_found(self, monkeypatch):
        """Returns empty string when gh CLI is not available."""
        from unittest.mock import patch
        from dev_team import _get_failing_pr_checks
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _get_failing_pr_checks("https://github.com/org/repo/pull/1")
        assert result == ""

    def test_returns_empty_on_timeout(self):
        from unittest.mock import patch
        import subprocess as sp
        from dev_team import _get_failing_pr_checks
        with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="gh", timeout=60)):
            result = _get_failing_pr_checks("https://github.com/org/repo/pull/1")
        assert result == ""

    def test_returns_failing_lines_when_present(self):
        from unittest.mock import patch, MagicMock
        from dev_team import _get_failing_pr_checks
        mock_result = MagicMock()
        mock_result.stdout = "build\tpass\nbuild-test\tfail\nlint\tpass\n"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _get_failing_pr_checks("https://github.com/org/repo/pull/1")
        assert "fail" in result
        assert "pass" not in result or "fail" in result

    def test_returns_empty_when_all_pass(self):
        from unittest.mock import patch, MagicMock
        from dev_team import _get_failing_pr_checks
        mock_result = MagicMock()
        mock_result.stdout = "build\tpass\nlint\tpass\n"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _get_failing_pr_checks("https://github.com/org/repo/pull/1")
        assert result == ""
