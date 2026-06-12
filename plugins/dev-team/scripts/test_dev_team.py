"""Tests for core step-machine components of dev_team.py.

Covers:
- exit_with_actions() — JSON array serialisation and exit-code 0
- compute_context_path() — base path resolution with/without DEV_TEAM_STATE_DIR
- Counter increment/reset logic — signoff_cycle_count, consecutive_failures,
  review_cycle_count
- signoff_build_result field — save/load round-trip
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

def _run_exit_with_actions(descriptors: list[dict]) -> subprocess.CompletedProcess:
    """Invoke exit_with_actions in a child process to isolate sys.exit."""
    descriptors_json = json.dumps(descriptors)
    script = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS_DIR)!r}); "
        f"import json; from dev_team import exit_with_actions; "
        f"exit_with_actions(json.loads({descriptors_json!r}))"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# exit_with_actions
# ---------------------------------------------------------------------------

class TestExitWithActions:
    def test_exits_with_code_0(self):
        result = _run_exit_with_actions([{"action": "done", "result": "success"}])
        assert result.returncode == 0

    def test_emits_json_array_on_stdout(self):
        descriptor = {"action": "done", "result": "success", "reason": "all clean"}
        result = _run_exit_with_actions([descriptor])
        parsed = json.loads(result.stdout.strip())
        assert isinstance(parsed, list)
        assert parsed == [descriptor]

    def test_single_item_wrapped_in_array(self):
        result = _run_exit_with_actions([{"action": "done"}])
        parsed = json.loads(result.stdout.strip())
        assert parsed == [{"action": "done"}]

    def test_serializes_nested_list_fields(self):
        descriptor = {
            "action": "spawn_agent",
            "agent": "developer",
            "skill": "developer-implement",
            "context_file": "/home/.dev-team/repo/ADR-123.md",
            "read_sections": ["Researcher Brief", "Review Notes"],
            "write_section": "Implementation Summary",
            "result_format": "success | failed",
        }
        result = _run_exit_with_actions([descriptor])
        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert parsed == [descriptor]

    def test_nothing_on_stderr(self):
        result = _run_exit_with_actions([{"action": "done", "result": "success"}])
        assert result.stderr == ""

    def test_empty_list_is_valid(self):
        result = _run_exit_with_actions([])
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == []

    def test_multiple_items_preserved_in_order(self):
        items = [
            {"action": "spawn_agent", "skill": "reviewer-sign-off"},
            {"action": "spawn_agent", "skill": "researcher-validate"},
            {"action": "run_script", "command": "bash build.sh"},
        ]
        result = _run_exit_with_actions(items)
        parsed = json.loads(result.stdout.strip())
        assert parsed == items


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
# _parse_approval_status
# ---------------------------------------------------------------------------

class TestParseApprovalStatus:
    def test_approved_json_returns_approved(self):
        from dev_team import _parse_approval_status
        assert _parse_approval_status('{"status": "approved"}') == "approved"

    def test_changes_requested_json_returns_changes_requested(self):
        from dev_team import _parse_approval_status
        assert _parse_approval_status('{"status": "changes_requested"}') == "changes_requested"

    def test_bare_approved_word_returns_approved(self):
        from dev_team import _parse_approval_status
        # Secondary heuristic when JSON parsing fails: "approved" keyword present
        assert _parse_approval_status("LGTM. Status: approved.") == "approved"

    def test_both_words_present_returns_changes_requested(self):
        from dev_team import _parse_approval_status
        # If both keywords appear, don't false-positive as approved
        content = "Previously approved but now changes_requested."
        assert _parse_approval_status(content) == "changes_requested"

    def test_unrecognised_content_defaults_to_changes_requested(self):
        from dev_team import _parse_approval_status
        assert _parse_approval_status("some random output") == "changes_requested"

    def test_json_with_pr_url_approved(self):
        from dev_team import _parse_approval_status
        content = '{"status": "approved", "pr_url": "https://github.com/org/repo/pull/1"}'
        assert _parse_approval_status(content) == "approved"


# ---------------------------------------------------------------------------
# _researcher_validated
# ---------------------------------------------------------------------------

class TestResearcherValidated:
    def test_validated_json_object_returns_true(self):
        from dev_team import _researcher_validated
        assert _researcher_validated('{"status": "validated"}') is True

    def test_failed_json_object_returns_false(self):
        from dev_team import _researcher_validated
        assert _researcher_validated('{"status": "failed"}') is False

    def test_validated_with_criteria_array_returns_true(self):
        from dev_team import _researcher_validated
        content = '{"status": "validated", "criteria": [{"criterion": "Tests pass", "status": "pass"}]}'
        assert _researcher_validated(content) is True

    def test_failed_with_criteria_array_returns_false(self):
        from dev_team import _researcher_validated
        content = '{"status": "failed", "criteria": [{"criterion": "Tests pass", "status": "fail", "finding": "not met"}]}'
        assert _researcher_validated(content) is False

    def test_validated_embedded_in_prose_returns_true(self):
        from dev_team import _researcher_validated
        # JSON on its own line within agent prose output
        content = 'All criteria were met.\n{"status": "validated", "criteria": []}'
        assert _researcher_validated(content) is True

    def test_failed_embedded_in_prose_returns_false(self):
        from dev_team import _researcher_validated
        content = 'Some criteria were not met.\n{"status": "failed", "criteria": []}'
        assert _researcher_validated(content) is False

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
# exit_with_actions — parallel flat array with mixed spawn_agent + run_script
# ---------------------------------------------------------------------------

class TestExitWithActionsParallel:
    def test_flat_array_with_spawn_and_run_script_items(self):
        items = [
            {"action": "spawn_agent", "agent": "task-runner", "skill": "reviewer-sign-off",
             "context_file": "/tmp/ctx.md", "read_sections": [],
             "write_section": "Signoff Review", "result_format": "success | failed"},
            {"action": "spawn_agent", "agent": "task-runner", "skill": "researcher-validate",
             "context_file": "/tmp/ctx.md", "read_sections": ["Researcher Brief"],
             "write_section": "Signoff Research", "result_format": "success | failed"},
            {"action": "run_script", "command": "bash validate-build.sh",
             "log_file": "/tmp/signoff.log", "result_format": "success | failed"},
        ]
        result = _run_exit_with_actions(items)
        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert isinstance(parsed, list)
        assert len(parsed) == 3

    def test_reviewer_item_in_flat_array(self):
        items = [
            {"action": "spawn_agent", "skill": "reviewer-sign-off"},
            {"action": "spawn_agent", "skill": "researcher-validate"},
            {"action": "run_script", "command": "bash build.sh", "log_file": "/tmp/build.log",
             "result_format": "success | failed"},
        ]
        result = _run_exit_with_actions(items)
        parsed = json.loads(result.stdout.strip())
        assert parsed[0]["skill"] == "reviewer-sign-off"

    def test_run_script_item_has_correct_fields(self):
        run_item = {"action": "run_script", "command": "bash test.sh",
                    "log_file": "/tmp/test.log", "result_format": "success | failed"}
        result = _run_exit_with_actions([run_item])
        parsed = json.loads(result.stdout.strip())
        assert parsed[0]["action"] == "run_script"
        assert parsed[0]["command"] == "bash test.sh"
        assert parsed[0]["log_file"] == "/tmp/test.log"


# ---------------------------------------------------------------------------
# message field in exit_with_actions items
# ---------------------------------------------------------------------------

class TestExitWithActionsMessage:
    def test_message_field_in_item_is_serialized(self):
        item = {
            "action": "spawn_agent",
            "message": "Developer is implementing.",
            "agent": "developer",
            "skill": "developer-implement",
        }
        result = _run_exit_with_actions([item])
        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert parsed[0]["message"] == "Developer is implementing."

    def test_item_without_message_still_valid(self):
        item = {"action": "done", "result": "success"}
        result = _run_exit_with_actions([item])
        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert "message" not in parsed[0]


# ---------------------------------------------------------------------------
# signoff_build_result field
# ---------------------------------------------------------------------------

class TestSignoffBuildResult:
    def make_sut(self, **kwargs):
        from dev_team import PipelineContext
        return PipelineContext(work_item_id="ADR-TEST", **kwargs)

    def test_defaults_to_empty_string(self):
        ctx = self.make_sut()
        assert ctx.signoff_build_result == ""

    def test_roundtrip_through_save_load(self, tmp_path):
        from dev_team import PipelineContext
        # script-runner returns "passed — log: <path>", not bare "passed"
        result = "passed — log: /home/user/.dev-team/ADR-123/logs/ADR-123-signoff-20240101T120000.log"
        ctx = PipelineContext(work_item_id="ADR-123", signoff_build_result=result)
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.signoff_build_result == result

    def test_failed_result_roundtrip(self, tmp_path):
        from dev_team import PipelineContext
        result = "failed — log: /home/user/.dev-team/ADR-123/logs/ADR-123-signoff-20240101T120000.log"
        ctx = PipelineContext(work_item_id="ADR-123", signoff_build_result=result)
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.signoff_build_result == result

    def test_passed_with_log_path_is_recognized(self):
        """script-runner returns 'passed — log: <path>'; startswith check must succeed."""
        result = "passed — log: /home/user/.dev-team/ADR-123/logs/ADR-123-signoff-20240101T120000.log"
        assert result.strip().startswith("passed")

    def test_failed_with_log_path_is_not_passed(self):
        """script-runner returns 'failed — log: <path>'; must not satisfy the passed check."""
        result = "failed — log: /home/user/.dev-team/ADR-123/logs/ADR-123-signoff-20240101T120000.log"
        assert not result.strip().startswith("passed")

    def test_empty_string_roundtrip(self, tmp_path):
        from dev_team import PipelineContext
        ctx = PipelineContext(work_item_id="ADR-123", signoff_build_result="")
        path = tmp_path / "ctx.md"
        ctx.save(path)
        loaded = PipelineContext.load(path)
        assert loaded.signoff_build_result == ""

    def test_reset_alongside_signoff_sections(self):
        """signoff_build_result can be reset to empty alongside signoff_review/research."""
        ctx = self.make_sut(
            signoff_review="approved",
            signoff_research="validated",
            signoff_build_result="passed — log: /tmp/test.log",
        )
        ctx.signoff_review = ""
        ctx.signoff_research = ""
        ctx.signoff_build_result = ""
        assert ctx.signoff_build_result == ""
        assert ctx.signoff_review == ""
        assert ctx.signoff_research == ""


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
            state="creating-pr",
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


# ---------------------------------------------------------------------------
# get-context-path.sh — slug extraction from various remote URL formats
# ---------------------------------------------------------------------------

import os
import shutil

GET_CONTEXT_PATH_SH = SCRIPTS_DIR / "get-context-path.sh"

# On Windows the system `bash` resolves to the WSL relay stub, which cannot
# execute POSIX scripts directly.  Prefer Git Bash if it is installed.
_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files\Git\bin\bash.exe",
]

def _find_bash() -> str | None:
    """Return the path to a usable bash executable, or None."""
    for candidate in _GIT_BASH_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    # Reject the Windows System32 WSL relay stub
    if found and "System32" not in found and "WindowsApps" not in found:
        return found
    return None

_BASH = _find_bash()

def _has_bash() -> bool:
    return _BASH is not None


def _make_env(tmp_path: Path, extra: dict | None = None) -> dict:
    """Build an env for running get-context-path.sh under test."""
    env = {**os.environ, "DEV_TEAM_STATE_DIR": str(tmp_path)}
    if extra:
        env.update(extra)
    return env


def _run_slug_extraction(remote_url: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the script with the remote URL injected via GIT_REMOTE_URL_OVERRIDE."""
    env = _make_env(tmp_path, {"GIT_REMOTE_URL_OVERRIDE": remote_url})
    return subprocess.run(
        [_BASH, str(GET_CONTEXT_PATH_SH), "ADR-123"],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
@pytest.mark.parametrize("remote_url,expected_slug", [
    ("https://github.com/org/repo.git",  "org/repo"),
    ("https://github.com/org/repo",       "org/repo"),
    ("git@github.com:org/repo.git",       "org/repo"),
    ("git@github.com:org/repo",           "org/repo"),
    ("https://github.com/acme-corp/cool-service.git", "acme-corp/cool-service"),
])
class TestGetContextPathShSlugExtraction:
    def test_slug_in_output_path(self, tmp_path, remote_url, expected_slug):
        result = _run_slug_extraction(remote_url, tmp_path)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # The output path must contain the expected slug as path components
        output = result.stdout.strip()
        # Normalise path separators for comparison
        slug_as_path = expected_slug.replace("/", __import__("os").sep)
        assert slug_as_path in output or expected_slug in output, (
            f"Expected slug {expected_slug!r} in output path {output!r}"
        )

    def test_exits_zero(self, tmp_path, remote_url, expected_slug):
        result = _run_slug_extraction(remote_url, tmp_path)
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_nothing_on_stderr(self, tmp_path, remote_url, expected_slug):
        result = _run_slug_extraction(remote_url, tmp_path)
        assert result.stderr == ""


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
class TestGetContextPathShErrorHandling:
    def test_exits_nonzero_when_git_fails(self, tmp_path):
        """Script should exit 1 and print to stderr when git fails.

        We achieve this by NOT setting GIT_REMOTE_URL_OVERRIDE and running in a
        directory that has no git remote, so `git remote get-url origin` fails.
        """
        empty_git_dir = tmp_path / "empty_repo"
        empty_git_dir.mkdir()
        # Initialise a bare repo with no remotes
        subprocess.run([_BASH, "-c", f"git init {str(empty_git_dir)!r}"],
                       capture_output=True, timeout=15)
        env = _make_env(tmp_path)
        result = subprocess.run(
            [_BASH, str(GET_CONTEXT_PATH_SH), "ADR-1"],
            capture_output=True, text=True, timeout=15, env=env,
            cwd=str(empty_git_dir),
        )
        assert result.returncode != 0
        assert result.stderr != ""

    def test_exits_nonzero_when_no_work_item_id(self, tmp_path):
        """Script should exit 1 with a usage message when called with no args."""
        env = _make_env(tmp_path)
        result = subprocess.run(
            [_BASH, str(GET_CONTEXT_PATH_SH)],
            capture_output=True, text=True, timeout=15, env=env,
        )
        assert result.returncode != 0
        assert "Usage" in result.stderr


# ---------------------------------------------------------------------------
# ParallelSteps
# ---------------------------------------------------------------------------

class _StubStep:
    """Minimal Step-like object for testing ParallelSteps."""

    def __init__(self, actions: list[dict], result: str) -> None:
        self._actions = actions
        self._result = result
        self.called = False

    def get_actions(self) -> list[dict]:
        return list(self._actions)

    def handle_results(self) -> str:
        self.called = True
        return self._result


class ConcreteParallelSteps:
    """Minimal concrete subclass of ParallelSteps for testing."""

    def __init__(self, steps):
        from dev_team import ParallelSteps
        # Build using composition since ParallelSteps is abstract
        self._ps = _ConcretePS(steps)

    def get_actions(self):
        return self._ps.get_actions()

    def handle_results(self):
        return self._ps.handle_results()


class _ConcretePS:
    """Concrete ParallelSteps for use in tests."""

    def __init__(self, steps):
        from dev_team import ParallelSteps
        # We can't directly instantiate ParallelSteps (abstract), so we subclass inline
        self._steps = steps

    def get_actions(self):
        all_actions = []
        for step in self._steps:
            all_actions.extend(step.get_actions())
        return all_actions

    def handle_results(self):
        child_monikers = [step.handle_results() for step in self._steps]
        return self.combine_results(child_monikers)

    def combine_results(self, child_monikers):
        if "failed" in child_monikers:
            return "failed"
        if "changes_requested" in child_monikers:
            return "changes_requested"
        return child_monikers[0] if child_monikers else "approved"


def _make_concrete_parallel(child_defs):
    """Build a concrete ParallelSteps-like with _StubStep children."""
    steps = [_StubStep(actions, result) for actions, result in child_defs]
    ps = _ConcretePS(steps)
    return ps, steps


class TestParallelStepsGetActions:
    def test_flat_list_equals_concatenation_of_children(self):
        a1 = {"action": "spawn_agent", "skill": "reviewer-sign-off"}
        a2 = {"action": "spawn_agent", "skill": "researcher-validate"}
        a3 = {"action": "run_script", "command": "bash build.sh"}
        s1 = _StubStep([a1], "approved")
        s2 = _StubStep([a2, a3], "validated")
        ps, _ = _make_concrete_parallel([([a1], "approved"), ([a2, a3], "validated")])
        actions = ps.get_actions()
        assert actions == [a1, a2, a3]

    def test_empty_children_produce_empty_list(self):
        ps, _ = _make_concrete_parallel([([], "approved")])
        assert ps.get_actions() == []

    def test_signoff_step_is_concrete_parallel(self):
        """SignoffStep (concrete ParallelSteps subclass) is instantiable."""
        from dev_team import SignoffStep, PipelineContext
        ctx = PipelineContext(work_item_id="ADR-TEST", pr_url="https://github.com/org/repo/pull/1")
        # SignoffStep is a concrete ParallelSteps — instantiation should not raise
        from pathlib import Path
        step = SignoffStep(ctx, Path("/tmp/ctx.md"), Path("/tmp/logs"))
        assert step is not None


class TestParallelStepsHandleResults:
    def test_each_child_handle_results_called(self):
        ps, steps = _make_concrete_parallel([
            ([{"a": 1}], "approved"),
            ([{"b": 2}], "approved"),
        ])
        ps.handle_results()
        assert steps[0].called
        assert steps[1].called

    def test_combine_results_failed_beats_all(self):
        ps, _ = _make_concrete_parallel([
            ([{"a": 1}], "failed"),
            ([{"b": 2}], "approved"),
        ])
        result = ps.handle_results()
        assert result == "failed"

    def test_combine_results_changes_requested_beats_approved(self):
        ps, _ = _make_concrete_parallel([
            ([{"a": 1}], "changes_requested"),
            ([{"b": 2}], "approved"),
        ])
        result = ps.handle_results()
        assert result == "changes_requested"

    def test_combine_results_all_approved_returns_first(self):
        ps, _ = _make_concrete_parallel([
            ([{"a": 1}], "approved"),
            ([{"b": 2}], "approved"),
        ])
        result = ps.handle_results()
        assert result == "approved"

    def test_failed_beats_changes_requested(self):
        ps, _ = _make_concrete_parallel([
            ([{"a": 1}], "changes_requested"),
            ([{"b": 2}], "failed"),
        ])
        result = ps.handle_results()
        assert result == "failed"


# ---------------------------------------------------------------------------
# Inline step (get_actions returns [])
# ---------------------------------------------------------------------------

class TestInlineStepDispatch:
    """The pipeline loop must advance through inline steps without calling
    exit_with_actions."""

    def _make_pipeline(self, ctx, context_path, step):
        """Build a minimal pipeline that contains a single inline step."""
        from dev_team import (
            DevTeamPipeline, WorkflowDefinition, StateMachine
        )
        workflow = WorkflowDefinition(
            transitions={
                "init": {"start": "testing"},
                "testing": {"done_ok": "done"},
            },
            terminal_states={"done"},
            initial_state="init",
        )
        pipeline = DevTeamPipeline.__new__(DevTeamPipeline)
        pipeline.ctx = ctx
        pipeline.context_path = context_path
        pipeline.log_dir = context_path.parent / "logs"
        pipeline.workflow = workflow
        pipeline.machine = StateMachine(workflow.transitions, initial="testing")
        pipeline.step_handlers = {"testing": step}
        return pipeline

    def test_inline_step_advances_without_exit(self, tmp_path):
        """get_actions=[] step: handle_results() called and trigger returned."""
        from dev_team import PipelineContext
        ctx = PipelineContext(work_item_id="ADR-TEST", state="testing")
        context_path = tmp_path / "ctx.md"
        ctx.save(context_path)

        step = _StubStep([], "done_ok")
        pipeline = self._make_pipeline(ctx, context_path, step)

        # _do_get_actions_and_exit should return the trigger directly (no sys.exit)
        trigger = pipeline._do_get_actions_and_exit(step)
        assert trigger == "done_ok"
        assert step.called


# ---------------------------------------------------------------------------
# CreatePrStep
# ---------------------------------------------------------------------------

class TestCreatePrStep:
    def _make_ctx(self, tmp_path, **kwargs):
        from dev_team import PipelineContext
        ctx = PipelineContext(work_item_id="ADR-TEST", **kwargs)
        context_path = tmp_path / "ctx.md"
        ctx.save(context_path)
        return ctx, context_path

    def test_get_actions_returns_descriptor_when_no_pr_url(self, tmp_path):
        from dev_team import CreatePrStep
        ctx, context_path = self._make_ctx(tmp_path, work_summaries=["# Summary"])
        step = CreatePrStep(ctx, context_path)
        actions = step.get_actions()
        assert len(actions) == 1
        assert actions[0]["skill"] == "developer-create-pr"

    def test_get_actions_returns_empty_when_pr_url_already_set(self, tmp_path):
        """Recovery re-entry: pr_url already in context — inline step."""
        from dev_team import CreatePrStep
        ctx, context_path = self._make_ctx(
            tmp_path,
            pr_url="https://github.com/org/repo/pull/5",
            work_summaries=["# Summary"],
        )
        step = CreatePrStep(ctx, context_path)
        assert step.get_actions() == []

    def test_handle_results_returns_pr_created_when_pr_url_already_set(self, tmp_path):
        """Inline path: pr_url was set before handle_results() — returns pr_created."""
        from dev_team import CreatePrStep
        ctx, context_path = self._make_ctx(
            tmp_path,
            pr_url="https://github.com/org/repo/pull/5",
        )
        step = CreatePrStep(ctx, context_path)
        trigger = step.handle_results()
        assert trigger == "pr_created"

    def test_handle_results_extracts_pr_url_from_section(self, tmp_path):
        """Normal dispatch: agent writes PR URL section; handle_results extracts it."""
        from dev_team import CreatePrStep
        ctx, context_path = self._make_ctx(tmp_path)
        # Simulate agent writing the PR URL section
        text = context_path.read_text(encoding="utf-8")
        text += "\n<!-- section:PR URL -->\n\nhttps://github.com/org/repo/pull/42\n"
        context_path.write_text(text, encoding="utf-8")

        step = CreatePrStep(ctx, context_path)
        trigger = step.handle_results()
        assert trigger == "pr_created"
        assert ctx.pr_url == "https://github.com/org/repo/pull/42"

    def test_handle_results_increments_failures_when_no_pr_url_written(self, tmp_path):
        """Failure path: agent ran but did not write PR URL."""
        from dev_team import CreatePrStep
        ctx, context_path = self._make_ctx(tmp_path)
        step = CreatePrStep(ctx, context_path)
        trigger = step.handle_results()
        # Still returns pr_created (fallback) but consecutive_failures incremented
        assert ctx.consecutive_failures == 1

    def test_descriptor_includes_required_fields(self, tmp_path):
        from dev_team import CreatePrStep
        ctx, context_path = self._make_ctx(tmp_path, work_summaries=["# Summary"])
        step = CreatePrStep(ctx, context_path)
        actions = step.get_actions()
        assert actions[0]["action"] == "spawn_agent"
        assert actions[0]["write_section"] == "PR URL"
        assert "context_file" in actions[0]
