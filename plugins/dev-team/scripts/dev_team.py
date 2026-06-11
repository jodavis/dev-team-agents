#!/usr/bin/env python3
"""dev-team pipeline step machine.

Entry point: main() — accepts a Jira work item ID and context file path, runs the
dev-team pipeline until an agent is needed, then exits with a JSON descriptor on
stdout (exit code 0). The orchestration loop in dev-team.md re-invokes this script
after each agent run.

To start fresh, delete the context file:
  ~/.dev-team/<repo-slug>/<work-item-id>.md
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

# Reconfigure stdout/stderr to UTF-8 early so that Unicode characters in agent
# output (e.g. arrows, bullets) don't crash on Windows cp1252 terminals.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MAX_FIX_ITERATIONS = 5
MAX_REVIEW_FIX_ITERATIONS = 3
CONSECUTIVE_FAILURES_THRESHOLD = 3
SIGNOFF_DEADLOCK_THRESHOLD = 2
REVIEW_LOOP_THRESHOLD = MAX_REVIEW_FIX_ITERATIONS


# ---------------------------------------------------------------------------
# Step-machine exit protocol
# ---------------------------------------------------------------------------

def exit_with_actions(descriptors: list[dict]) -> NoReturn:
    """Emit a JSON array of action descriptors on stdout and exit 0.

    Called when the pipeline needs one or more agents/scripts to run. The
    orchestration loop in dev-team.md parses this array, dispatches each item
    in parallel, then re-invokes the script.
    """
    print(json.dumps(descriptors), flush=True)
    sys.exit(0)


def compute_context_path(work_item_id: str, repo_slug: str) -> Path:
    """Compute the context file path for a work item.

    Base: DEV_TEAM_STATE_DIR env var, or ~/.dev-team if unset.
    Full path: <base>/<repo_slug>/<work_item_id>.md

    This helper is used by dev-team.md before invoking the script. The script
    itself receives --context-file as a required argument with no fallback.
    """
    base_env = os.environ.get("DEV_TEAM_STATE_DIR")
    base = Path(base_env) if base_env else Path.home() / ".dev-team"
    return base / repo_slug / f"{work_item_id}.md"


# ---------------------------------------------------------------------------
# Counter helpers
# ---------------------------------------------------------------------------

def _apply_counter_updates(ctx: "PipelineContext", step_name: str, trigger: str) -> None:
    """Update pipeline counters after a step returns a trigger.

    Called by DevTeamPipeline.run() after a step returns normally (not via
    exit_with_actions). The step_name is the state that just completed.
    """
    if step_name == "reviewing":
        ctx.review_cycle_count += 1
    elif step_name == "signoff":
        if trigger == "changes_requested":
            ctx.signoff_cycle_count += 1
        elif trigger == "approved":
            ctx.signoff_cycle_count = 0
            ctx.review_cycle_count = 0


def _handle_agent_failure(ctx: "PipelineContext") -> None:
    """Increment consecutive_failures after an agent return was empty or unparseable."""
    ctx.consecutive_failures += 1


def _handle_agent_success(ctx: "PipelineContext") -> None:
    """Reset consecutive_failures after any successful agent return."""
    ctx.consecutive_failures = 0


# ---------------------------------------------------------------------------
# Workflow definition (parsed from a Mermaid stateDiagram-v2 file)
# ---------------------------------------------------------------------------

@dataclass
class WorkflowDefinition:
    transitions: dict[str, dict[str, str]]
    terminal_states: set[str]
    initial_state: str


def parse_workflow(path: Path) -> WorkflowDefinition:
    """Parse a Mermaid stateDiagram-v2 block from a markdown file.

    Recognises three line forms inside the diagram:
      [*] --> StateA          → initial state
      StateA --> [*]          → terminal state
      StateA --> StateB : t   → transition with trigger t
    """
    text = path.read_text(encoding="utf-8")

    match = re.search(r"```mermaid\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        raise ValueError(f"No mermaid fenced block found in {path}")

    transitions: dict[str, dict[str, str]] = {}
    terminal_states: set[str] = set()
    initial_state: str | None = None

    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if "-->" not in line:
            continue

        m = re.match(r"^([\w-]+)\s+-->\s+\[\*\]$", line)
        if m:
            terminal_states.add(m.group(1))
            continue

        m = re.match(r"^\[\*\]\s+-->\s+([\w-]+)$", line)
        if m:
            initial_state = m.group(1)
            continue

        m = re.match(r"^([\w-]+)\s+-->\s+([\w-]+)\s*:\s*([\w-]+)$", line)
        if m:
            src, dst, trigger = m.group(1), m.group(2), m.group(3)
            transitions.setdefault(src, {})[trigger] = dst
            continue

    if initial_state is None:
        raise ValueError(f"No initial state ([*] --> ...) found in {path}")

    return WorkflowDefinition(
        transitions=transitions,
        terminal_states=terminal_states,
        initial_state=initial_state,
    )


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class StateMachine:
    """Simple state machine backed by a dict-of-dicts transition table."""

    def __init__(self, transitions: dict[str, dict[str, str]], initial: str) -> None:
        self._transitions = transitions
        self.state = initial

    def transition(self, trigger: str) -> str:
        """Advance to the next state via trigger. Returns new state."""
        available = self._transitions.get(self.state, {})
        if trigger not in available:
            raise ValueError(
                f"Invalid trigger '{trigger}' from state '{self.state}'. "
                f"Available: {list(available)}"
            )
        self.state = available[trigger]
        return self.state


# ---------------------------------------------------------------------------
# Pipeline context (serializable state)
# ---------------------------------------------------------------------------

@dataclass
class PipelineContext:
    """All mutable state for a dev-team pipeline run, persisted across invocations."""

    work_item_id: str
    spec_path: str = ""
    state: str = "init"
    brief: str = ""
    work_summaries: list[str] = field(default_factory=list)
    fix_iteration: int = 0
    review_fix_iteration: int = 0
    pr_url: str = ""
    review_notes: str = ""
    last_failure: str = ""
    build_log: str = ""
    test_log: str = ""
    debug_report: str = ""
    signoff_review: str = ""
    signoff_research: str = ""
    signoff_build_result: str = ""
    validate_result: str = ""
    # Counters tracked for troubleshooter trigger conditions
    signoff_cycle_count: int = 0
    consecutive_failures: int = 0
    review_cycle_count: int = 0
    troubleshooter_input: str = ""
    # Tracks which agent spawn is currently pending (set before exit_with_actions)
    pending_agent: str = ""
    started: datetime.datetime = field(default_factory=datetime.datetime.now)
    last_updated: datetime.datetime = field(default_factory=datetime.datetime.now)

    def save(self, path: Path) -> None:
        """Write context to a markdown file with YAML frontmatter and named sections."""
        self.last_updated = datetime.datetime.now()

        lines = [
            "---",
            f"state: {self.state}",
            f"work_item_id: {self.work_item_id}",
            f"spec_path: {self.spec_path}",
            f"fix_iteration: {self.fix_iteration}",
            f"review_fix_iteration: {self.review_fix_iteration}",
            f"pr_url: {self.pr_url}",
            f"build_log: {self.build_log}",
            f"test_log: {self.test_log}",
            f"signoff_cycle_count: {self.signoff_cycle_count}",
            f"consecutive_failures: {self.consecutive_failures}",
            f"review_cycle_count: {self.review_cycle_count}",
            f"troubleshooter_input: {self.troubleshooter_input}",
            f"pending_agent: {self.pending_agent}",
            f"started: {self.started.isoformat()}",
            f"last_updated: {self.last_updated.isoformat()}",
            "---",
            "",
            f"# {self.work_item_id} Dev Team Context",
        ]

        if self.debug_report:
            lines += ["", "<!-- section:Debug Report -->", "", self.debug_report.strip()]

        if self.brief:
            lines += ["", "<!-- section:Researcher Brief -->", "", self.brief.strip()]

        if self.work_summaries:
            lines += ["", "<!-- section:Implementation Summary -->", "", self.work_summaries[0].strip()]
            for i, summary in enumerate(self.work_summaries[1:], start=1):
                lines += ["", f"<!-- section:Fix {i} -->", "", summary.strip()]

        if self.review_notes:
            lines += ["", "<!-- section:Review Notes -->", "", self.review_notes.strip()]

        if self.last_failure:
            lines += ["", "<!-- section:Last Failure -->", "", self.last_failure.strip()]

        if self.signoff_review:
            lines += ["", "<!-- section:Signoff Review -->", "", self.signoff_review.strip()]

        if self.signoff_research:
            lines += ["", "<!-- section:Signoff Research -->", "", self.signoff_research.strip()]

        if self.signoff_build_result:
            lines += ["", "<!-- section:Signoff Build Result -->", "", self.signoff_build_result.strip()]

        if self.validate_result:
            lines += ["", "<!-- section:Validate Result -->", "", self.validate_result.strip()]

        log_links: list[str] = []
        if self.build_log:
            log_links.append(f"- Build: {self.build_log}")
        if self.test_log:
            log_links.append(f"- Tests: {self.test_log}")
        if log_links:
            lines += ["", "<!-- section:Logs -->", ""] + log_links

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "PipelineContext":
        """Load context from a markdown file previously written by save()."""
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)

        ctx = cls(
            work_item_id=meta.get("work_item_id", ""),
            spec_path=meta.get("spec_path", ""),
            state=meta.get("state", "init"),
            fix_iteration=int(meta.get("fix_iteration", 0)),
            review_fix_iteration=int(meta.get("review_fix_iteration", 0)),
            pr_url=meta.get("pr_url", ""),
            build_log=meta.get("build_log", ""),
            test_log=meta.get("test_log", ""),
            signoff_cycle_count=int(meta.get("signoff_cycle_count", 0)),
            consecutive_failures=int(meta.get("consecutive_failures", 0)),
            review_cycle_count=int(meta.get("review_cycle_count", 0)),
            troubleshooter_input=meta.get("troubleshooter_input", ""),
            pending_agent=meta.get("pending_agent", ""),
        )

        try:
            ctx.started = datetime.datetime.fromisoformat(meta["started"])
            ctx.last_updated = datetime.datetime.fromisoformat(meta["last_updated"])
        except (KeyError, ValueError):
            pass

        sections = _parse_sections(body)
        ctx.debug_report = sections.get("Debug Report", "")
        ctx.brief = sections.get("Researcher Brief", "")

        work_summaries: list[str] = []
        if "Implementation Summary" in sections:
            work_summaries.append(sections["Implementation Summary"])
        i = 1
        while f"Fix {i}" in sections:
            work_summaries.append(sections[f"Fix {i}"])
            i += 1
        ctx.work_summaries = work_summaries
        ctx.review_notes = sections.get("Review Notes", "")
        ctx.last_failure = sections.get("Last Failure", "")
        ctx.signoff_review = sections.get("Signoff Review", "")
        ctx.signoff_research = sections.get("Signoff Research", "")
        ctx.signoff_build_result = sections.get("Signoff Build Result", "")
        ctx.validate_result = sections.get("Validate Result", "")

        return ctx


def _parse_sections(body: str) -> dict[str, str]:
    """Split a markdown body into {heading: content} by '<!-- section:Name -->' sentinels."""
    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in body.split("\n"):
        if line.startswith("<!-- section:") and line.endswith(" -->"):
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines).strip()
            current_heading = line[len("<!-- section:"):-len(" -->")].strip()
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines).strip()

    return sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_failing_pr_checks(pr_url: str) -> str:
    """Run `gh pr checks <pr_url>` and return output for failing checks.

    Returns a string with failing check lines, or empty string if all pass or
    if gh is unavailable / the command fails.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", pr_url],
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout + result.stderr
        # Return lines that indicate a failing check (non-passing status)
        failing_lines = [
            line for line in output.splitlines()
            if any(word in line.lower() for word in ("fail", "error", "x "))
        ]
        return "\n".join(failing_lines) if failing_lines else ""
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return ""


def _commit_and_push(work_item_id: str) -> None:
    """Push the current branch. The developer is expected to have already committed."""
    try:
        subprocess.run(["git", "add", "-A"], check=True, cwd=REPO_ROOT, capture_output=True)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT, capture_output=True,
        )
        if diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", f"{work_item_id}: uncommitted changes at validation"],
                check=True, cwd=REPO_ROOT, capture_output=True,
            )
        subprocess.run(
            ["git", "push", "origin", "HEAD"],
            check=True, cwd=REPO_ROOT, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Warning: git commit/push failed (continuing): {e.stderr}", flush=True)


def parse_json_output(text: str) -> dict:
    """Extract the last parseable JSON object from agent output text."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    for block in reversed(re.findall(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)):
        try:
            result = json.loads(block.strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    return {}


def _researcher_validated(content: str) -> bool:
    """Return True if researcher-validate reported success.

    Expects a JSON object with a "status" field ("validated" | "failed"),
    matching the standardized skill output format.
    """
    result = parse_json_output(content)
    status = result.get("status", "")
    if status == "validated":
        return True
    if status == "failed":
        return False
    # Unrecognised format — treat as not validated so signoff retries.
    return False


def _parse_approval_status(content: str) -> str:
    """Extract approval status from agent output.

    Returns "approved" or "changes_requested". Falls back to scanning the
    content for the word "approved" if JSON parsing fails, to guard against
    minor output format deviations.
    """
    result = parse_json_output(content)
    status = result.get("status", "")
    if status in ("approved", "changes_requested"):
        return status
    # Secondary heuristic: look for the keyword in the text
    lower = content.lower()
    if "approved" in lower and "changes_requested" not in lower:
        return "approved"
    return "changes_requested"


def _troubleshooter_descriptor(
    trigger: str, context_path: Path, ctx: "PipelineContext"
) -> dict:
    """Build the exit descriptor for a troubleshooter spawn."""
    return {
        "action": "spawn_agent",
        "message": f"Pipeline issue detected (trigger: {trigger}). Troubleshooter is intervening.",
        "skill": "troubleshooter",
        "trigger": trigger,
        "context_file": str(context_path),
        "cycle_count": ctx.consecutive_failures if trigger == "consecutive_failures"
                       else ctx.signoff_cycle_count if trigger == "signoff_deadlock"
                       else ctx.review_cycle_count,
    }


def _check_and_trigger_troubleshooter(
    trigger: str,
    threshold: int,
    count: int,
    ctx: "PipelineContext",
    context_path: Path,
) -> None:
    """Exit with a troubleshooter descriptor if count has reached threshold."""
    if count >= threshold:
        ctx.save(context_path)
        exit_with_actions([_troubleshooter_descriptor(trigger, context_path, ctx)])


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class Step(ABC):
    """A single phase of the dev-team pipeline."""

    handles: str

    @abstractmethod
    def get_actions(self) -> list[dict]:
        """Return action descriptors to dispatch. Empty list means inline step."""
        ...

    @abstractmethod
    def handle_results(self, results: list[str]) -> str:
        """Accept one-line results (one per action) and return a trigger moniker."""
        ...

    def run(self, ctx: "PipelineContext") -> str:
        """Deprecated shim — subclasses must implement get_actions/handle_results."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement get_actions() and handle_results() "
            "instead of run()"
        )


class FindSpecStep(Step):
    handles = "spec-finding"

    def __init__(self, ctx: "PipelineContext") -> None:
        self._ctx = ctx

    def get_actions(self) -> list[dict]:
        """Inline step — no actions needed."""
        return []

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if ctx.spec_path:
            print("Spec path already set — skipping.", flush=True)
            return "spec_found"
        print(f"Searching for spec for {ctx.work_item_id}...", flush=True)
        spec_file = find_spec_file(ctx.work_item_id)
        ctx.spec_path = str(spec_file.relative_to(REPO_ROOT))
        print(f"Found {spec_file}", flush=True)
        return "spec_found"


class DebugStep(Step):
    handles = "debugging"

    _PENDING_KEY = "debug"

    def __init__(self, ctx: "PipelineContext", context_path: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        if ctx.debug_report:
            # Result already available — inline step
            return []
        print(f"Debugger is investigating {ctx.work_item_id}...", flush=True)
        return [{
            "action": "spawn_agent",
            "message": f"Debugger is investigating {ctx.work_item_id}.",
            "agent": "debugger",
            "skill": "debugger-investigate",
            "context_file": str(self._context_path),
            "args": ctx.work_item_id,
            "read_sections": [],
            "write_section": "Debug Report",
            "result_format": "reproduced | not_reproduced",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if ctx.debug_report:
            _handle_agent_success(ctx)
            if "# Debug report for" not in ctx.debug_report:
                ctx.last_failure = f"Bug could not be reproduced.\n\n{ctx.debug_report}"
                return "reproduction_failed"
            print("Debugging complete.", flush=True)
            return "debug_done"
        # Agent ran but wrote nothing
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        # If we get here, consecutive_failures has not hit threshold — return failure trigger
        return "reproduction_failed"


class ResearchStep(Step):
    handles = "researching"

    _PENDING_KEY = "research"

    def __init__(self, skill: str, ctx: "PipelineContext", context_path: Path) -> None:
        self._skill = skill
        self._ctx = ctx
        self._context_path = context_path

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        if ctx.brief:
            return []
        print(f"Researcher is planning work for {ctx.work_item_id}...", flush=True)
        read_sections = ["Debug Report"] if ctx.debug_report else []
        return [{
            "action": "spawn_agent",
            "message": f"Researcher is planning work for {ctx.work_item_id}.",
            "agent": "researcher",
            "skill": self._skill,
            "context_file": str(self._context_path),
            "args": f"{ctx.work_item_id} {ctx.spec_path}",
            "read_sections": read_sections,
            "write_section": "Researcher Brief",
            "result_format": "briefed | failed",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if ctx.brief:
            _handle_agent_success(ctx)
            print("Research complete.", flush=True)
            return "research_done"
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "research_done"


class ImplementStep(Step):
    handles = "implementing"

    _PENDING_KEY = "implement"

    def __init__(self, ctx: "PipelineContext", context_path: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        if ctx.work_summaries:
            return []
        print(f"Developer is implementing {ctx.work_item_id}...", flush=True)
        return [{
            "action": "spawn_agent",
            "message": "Researcher has written the task brief. Developer is now implementing.",
            "agent": "developer",
            "skill": "developer-implement",
            "args": ctx.work_item_id,
            "context_file": str(self._context_path),
            "read_sections": ["Researcher Brief"],
            "write_section": "Implementation Summary",
            "result_format": "implemented | failed | needs_clarification",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if ctx.work_summaries:
            _handle_agent_success(ctx)
            print("Implementation already complete in context — skipping.", flush=True)
            return "impl_done"
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "impl_done"


class ValidateStep(Step):
    handles = "validating"

    _PENDING_KEY = "validate"

    def __init__(self, ctx: "PipelineContext", context_path: Path, log_dir: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path
        self._log_dir = log_dir

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        if ctx.validate_result:
            return []
        self._log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        log_path = self._log_dir / f"{ctx.work_item_id}-validate-{timestamp}.log"
        ctx.build_log = str(log_path)
        ext = ".cmd" if sys.platform == "win32" else ".sh"
        validate_script = REPO_ROOT / "scripts" / f"validate{ext}"
        command = f'cmd /c "{validate_script}"' if sys.platform == "win32" else f'bash "{validate_script}"'
        print(f"Spawning script-runner to validate {ctx.work_item_id}...", flush=True)
        return [{
            "action": "run_script",
            "message": "Running build and test validation.",
            "command": command,
            "log_file": str(log_path),
            "write_section": "Validate Result",
            "result_format": "passed | failed",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if ctx.validate_result:
            result = ctx.validate_result.strip()
            ctx.validate_result = ""
            ctx.pending_agent = ""
            if result == "passed":
                print("Validation passed.", flush=True)
                ctx.last_failure = ""
                _commit_and_push(ctx.work_item_id)
                return "clean"
            print(f"Validation FAILED. Log: {ctx.build_log}", flush=True)
            ctx.last_failure = (
                f"Build or test failures.\n\n"
                f"Full log (read this for details): {ctx.build_log}"
            )
            return "build_failed"
        # Script-runner ran but wrote nothing
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "build_failed"


_REDISPATCH = "_redispatch"
"""Sentinel trigger returned by handle_results() when the step needs another dispatch
round before a final trigger can be produced. The pipeline loop handles this by calling
get_actions() again on the same step without advancing the state machine."""


class ReviewStep(Step):
    handles = "reviewing"

    _PENDING_CREATE_PR = "create-pr"
    _PENDING_REVIEW = "reviewer-review"

    def __init__(self, ctx: "PipelineContext", context_path: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        # Sub-step 1: create PR
        if not ctx.pr_url:
            print(f"Developer is creating PR for {ctx.work_item_id}...", flush=True)
            read_sections = ["Researcher Brief", "Implementation Summary"]
            for i in range(1, len(ctx.work_summaries)):
                read_sections.append(f"Fix {i}")
            return [{
                "action": "spawn_agent",
                "message": "Implementation complete. Developer is creating a pull request.",
                "agent": "developer",
                "skill": "developer-create-pr",
                "args": ctx.work_item_id,
                "context_file": str(self._context_path),
                "read_sections": read_sections,
                "write_section": "PR URL",
                "result_format": "pr_created | failed",
            }]
        # Sub-step 2: review (or inline if notes already present)
        if ctx.review_notes:
            return []
        print(f"Reviewer is reviewing {ctx.work_item_id}...", flush=True)
        return [{
            "action": "spawn_agent",
            "message": "Pull request created. Reviewer is reviewing the changes.",
            "agent": "reviewer",
            "skill": "reviewer-review",
            "context_file": str(self._context_path),
            "read_sections": ["Researcher Brief"],
            "write_section": "Review Notes",
            "result_format": "approved | changes_requested",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if not ctx.pr_url:
            # Just ran create-PR — try to extract pr_url
            text = self._context_path.read_text(encoding="utf-8")
            _, body = _parse_frontmatter(text)
            sections = _parse_sections(body)
            pr_url_section = sections.get("PR URL", "")
            if pr_url_section:
                m = re.search(r"https://github\.com/[^\s]+/pull/\d+", pr_url_section)
                if m:
                    ctx.pr_url = m.group(0)
                    ctx.save(self._context_path)
            if not ctx.pr_url:
                _handle_agent_failure(ctx)
                _check_and_trigger_troubleshooter(
                    "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                    ctx.consecutive_failures, ctx, self._context_path,
                )
            # Signal the loop to dispatch again (review sub-step) before transitioning
            return _REDISPATCH

        # Review sub-step result
        if ctx.review_notes:
            _handle_agent_success(ctx)
            status = _parse_approval_status(ctx.review_notes)
            if status == "approved":
                print("Review approved.", flush=True)
                return "approved"
            print("Reviewer requested changes.", flush=True)
            return "changes_requested"
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "changes_requested"


class ParallelSteps(Step):
    """Composite step that dispatches multiple child steps in parallel.

    get_actions() concatenates all children's actions into a single flat list.
    handle_results() splits results by each child's action count, calls each
    child's handle_results(), and passes the resulting monikers to combine_results().
    """

    def __init__(self, steps: list["Step"]) -> None:
        self._steps = steps
        self._action_counts: list[int] = []

    def get_actions(self) -> list[dict]:
        all_actions: list[dict] = []
        self._action_counts = []
        for step in self._steps:
            actions = step.get_actions()
            self._action_counts.append(len(actions))
            all_actions.extend(actions)
        return all_actions

    def handle_results(self, results: list[str]) -> str:
        child_monikers: list[str] = []
        offset = 0
        for step, count in zip(self._steps, self._action_counts):
            child_results = results[offset: offset + count]
            offset += count
            moniker = step.handle_results(child_results)
            child_monikers.append(moniker)
        return self.combine_results(child_monikers)

    def combine_results(self, child_monikers: list[str]) -> str:
        """Combine child monikers: 'failed' > 'changes_requested' > first moniker."""
        if "failed" in child_monikers:
            return "failed"
        if "changes_requested" in child_monikers:
            return "changes_requested"
        return child_monikers[0] if child_monikers else "approved"


class ReviewerSignOffStep(Step):
    """Wraps the reviewer-sign-off spawn for use inside ParallelSteps."""

    def __init__(self, ctx: "PipelineContext", context_path: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path

    def get_actions(self) -> list[dict]:
        return [{
            "action": "spawn_agent",
            "agent": "task-runner",
            "skill": "reviewer-sign-off",
            "context_file": str(self._context_path),
            "read_sections": ["Researcher Brief"],
            "write_section": "Signoff Review",
            "result_format": "approved | changes_requested",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if ctx.signoff_review:
            _handle_agent_success(ctx)
            return _parse_approval_status(ctx.signoff_review)
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "changes_requested"


class ResearcherSignOffStep(Step):
    """Wraps the researcher-validate spawn for use inside ParallelSteps."""

    def __init__(self, ctx: "PipelineContext", context_path: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        read_sections = ["Researcher Brief", "Implementation Summary"]
        for i in range(1, len(ctx.work_summaries)):
            read_sections.append(f"Fix {i}")
        return [{
            "action": "spawn_agent",
            "agent": "task-runner",
            "skill": "researcher-validate",
            "context_file": str(self._context_path),
            "read_sections": read_sections,
            "write_section": "Signoff Research",
            "result_format": "validated | failed",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if ctx.signoff_research:
            _handle_agent_success(ctx)
            return "approved" if _researcher_validated(ctx.signoff_research) else "failed"
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "failed"


class BuildValidationStep(Step):
    """Wraps the wait-pr-checks run_script for use inside ParallelSteps."""

    def __init__(self, ctx: "PipelineContext", context_path: Path, log_dir: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path
        self._log_dir = log_dir

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        self._log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        log_path = self._log_dir / f"{ctx.work_item_id}-signoff-{timestamp}.log"
        ctx.build_log = str(log_path)
        scripts_dir = Path(__file__).parent
        wait_script = scripts_dir / "wait-pr-checks.sh"
        command = f'bash "{wait_script}" "{ctx.pr_url}"'
        return [{
            "action": "run_script",
            "command": command,
            "log_file": str(log_path),
            "write_section": "Signoff Build Result",
            "result_format": "passed | failed",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        if ctx.signoff_build_result:
            _handle_agent_success(ctx)
            return "approved" if ctx.signoff_build_result.strip().startswith("passed") else "failed"
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "failed"


class SignoffStep(ParallelSteps):
    handles = "signoff"

    def __init__(self, ctx: "PipelineContext", context_path: Path, log_dir: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path
        self._log_dir = log_dir
        super().__init__([
            ReviewerSignOffStep(ctx, context_path),
            ResearcherSignOffStep(ctx, context_path),
            BuildValidationStep(ctx, context_path, log_dir),
        ])

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        # Push first so the reviewer can see the latest commits.
        _commit_and_push(ctx.work_item_id)
        print(f"Spawning reviewer, researcher, and build/test in parallel for "
              f"{ctx.work_item_id}...", flush=True)
        return super().get_actions()

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        trigger = super().handle_results(results)

        # Build the failure summary for downstream steps
        failures: list[str] = []
        if not ctx.signoff_build_result.strip().startswith("passed"):
            if ctx.signoff_build_result:
                failures.append(
                    f"Build/test validation failed. Log: {ctx.build_log}\n"
                    f"Script result: {ctx.signoff_build_result.strip()}"
                )
        if _parse_approval_status(ctx.signoff_review) != "approved":
            if ctx.signoff_review:
                failures.append(f"Reviewer sign-off:\n{ctx.signoff_review}")
        if not _researcher_validated(ctx.signoff_research):
            if ctx.signoff_research:
                failures.append(f"Research validation:\n{ctx.signoff_research}")

        # Reset sub-step sections for the next signoff cycle
        ctx.signoff_review = ""
        ctx.signoff_research = ""
        ctx.signoff_build_result = ""
        ctx.pending_agent = ""

        if failures or trigger != "approved":
            ctx.review_notes = "\n\n---\n\n".join(failures) if failures else "Signoff failed."
            ctx.last_failure = ctx.review_notes
            print("Signoff found issues; requesting further changes.", flush=True)
            return "changes_requested"

        ctx.last_failure = ""
        print("Signoff approved.", flush=True)
        return "approved"

    def combine_results(self, child_monikers: list[str]) -> str:
        """Signoff: 'failed' > 'changes_requested' > 'approved'."""
        if "failed" in child_monikers:
            return "failed"
        if "changes_requested" in child_monikers:
            return "changes_requested"
        return "approved"


class FixStep(Step):
    handles = "fixing"

    def __init__(self, ctx: "PipelineContext", context_path: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        completed = 1 + ctx.fix_iteration + ctx.review_fix_iteration
        if len(ctx.work_summaries) > completed:
            return []
        if ctx.fix_iteration >= MAX_FIX_ITERATIONS:
            return []
        write_section = f"Fix {completed}"
        print(
            f"Invoking developer to fix "
            f"(iteration {ctx.fix_iteration + 1} of {MAX_FIX_ITERATIONS})...",
            flush=True,
        )
        read_sections = ["Researcher Brief", "Last Failure"]
        if ctx.work_summaries:
            read_sections.append("Implementation Summary")
        for i in range(1, len(ctx.work_summaries)):
            read_sections.append(f"Fix {i}")
        return [{
            "action": "spawn_agent",
            "message": (
                f"Build or tests failed. Developer is fixing "
                f"(iteration {ctx.fix_iteration + 1} of {MAX_FIX_ITERATIONS})."
            ),
            "agent": "developer",
            "skill": "developer-fix",
            "args": ctx.work_item_id,
            "context_file": str(self._context_path),
            "read_sections": read_sections,
            "write_section": write_section,
            "result_format": "fixed | failed",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        completed = 1 + ctx.fix_iteration + ctx.review_fix_iteration
        if len(ctx.work_summaries) > completed:
            _handle_agent_success(ctx)
            ctx.fix_iteration += 1
            return "fix_done"
        if ctx.fix_iteration >= MAX_FIX_ITERATIONS:
            print(
                f"Error: still failing after {MAX_FIX_ITERATIONS} fix iterations. "
                f"Manual intervention needed.",
                file=sys.stderr,
            )
            return "max_retries"
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "fix_done"


class FixPrStep(Step):
    handles = "fixing-pr"

    def __init__(self, ctx: "PipelineContext", context_path: Path) -> None:
        self._ctx = ctx
        self._context_path = context_path

    def get_actions(self) -> list[dict]:
        ctx = self._ctx
        completed = 1 + ctx.fix_iteration + ctx.review_fix_iteration
        if len(ctx.work_summaries) > completed:
            return []
        if ctx.review_fix_iteration >= MAX_REVIEW_FIX_ITERATIONS:
            return []
        write_section = f"Fix {completed}"
        print(
            f"Invoking developer to address review comments "
            f"(iteration {ctx.review_fix_iteration + 1} of {MAX_REVIEW_FIX_ITERATIONS})...",
            flush=True,
        )
        read_sections = ["Researcher Brief", "Review Notes", "Implementation Summary"]
        for i in range(1, len(ctx.work_summaries)):
            read_sections.append(f"Fix {i}")
        # When a PR exists, include failing GitHub Actions check output
        if ctx.pr_url:
            pr_checks_output = _get_failing_pr_checks(ctx.pr_url)
            if pr_checks_output:
                ctx.last_failure = (
                    f"{ctx.review_notes}\n\n"
                    f"Failing GitHub Actions checks:\n```\n{pr_checks_output}\n```"
                )
        return [{
            "action": "spawn_agent",
            "message": (
                f"Review requested changes. Developer is addressing review comments "
                f"(iteration {ctx.review_fix_iteration + 1} of {MAX_REVIEW_FIX_ITERATIONS})."
            ),
            "agent": "developer",
            "skill": "developer-fix",
            "args": ctx.work_item_id,
            "context_file": str(self._context_path),
            "read_sections": read_sections,
            "write_section": write_section,
            "result_format": "fixed | failed",
        }]

    def handle_results(self, results: list[str]) -> str:
        ctx = self._ctx
        completed = 1 + ctx.fix_iteration + ctx.review_fix_iteration
        if len(ctx.work_summaries) > completed:
            _handle_agent_success(ctx)
            ctx.review_fix_iteration += 1
            ctx.review_notes = ""  # ensure ReviewStep re-runs reviewer on next cycle
            return "fix_done"
        if ctx.review_fix_iteration >= MAX_REVIEW_FIX_ITERATIONS:
            print(
                f"Error: still failing review after {MAX_REVIEW_FIX_ITERATIONS} "
                f"review fix iterations. Manual intervention needed.",
                file=sys.stderr,
            )
            return "max_retries"
        _handle_agent_failure(ctx)
        _check_and_trigger_troubleshooter(
            "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
            ctx.consecutive_failures, ctx, self._context_path,
        )
        return "fix_done"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class DevTeamPipeline:
    """Drives the dev-team state machine from init (or a resumed state) to done."""

    def __init__(
        self,
        ctx: PipelineContext,
        context_path: Path,
        log_dir: Path,
        workflow: WorkflowDefinition,
        research_skill: str,
        results: list[str] | None = None,
    ) -> None:
        self.ctx = ctx
        self.context_path = context_path
        self.log_dir = log_dir
        self.workflow = workflow
        self.results = results  # pre-parsed --results list, or None
        self.machine = StateMachine(workflow.transitions, initial=ctx.state)
        self.step_handlers: dict[str, Step] = {
            "spec-finding": FindSpecStep(ctx),
            "debugging": DebugStep(ctx, context_path),
            "researching": ResearchStep(research_skill, ctx, context_path),
            "implementing": ImplementStep(ctx, context_path),
            "validating": ValidateStep(ctx, context_path, log_dir),
            "fixing": FixStep(ctx, context_path),
            "reviewing": ReviewStep(ctx, context_path),
            "signoff": SignoffStep(ctx, context_path, log_dir),
            "fixing-pr": FixPrStep(ctx, context_path),
        }

    def _dispatch_step(self, step: Step) -> str:
        """Dispatch a step: get actions, exit if non-empty, else return trigger inline.

        If results are available (--results provided), call handle_results() instead
        of exiting.

        Returns the trigger string once the step has fully resolved. For _REDISPATCH,
        loops until a real trigger is obtained.
        """
        # If --results provided on this invocation, process them for the current step
        if self.results is not None:
            results = self.results
            self.results = None  # consume once
            trigger = step.handle_results(results)
            if trigger == _REDISPATCH:
                # Need another dispatch round — get_actions() returns the next batch
                return self._do_get_actions_and_exit(step)
            return trigger

        # No results yet — check for inline step or dispatch
        return self._do_get_actions_and_exit(step)

    def _do_get_actions_and_exit(self, step: Step) -> str:
        """Call get_actions(); exit if non-empty; otherwise call handle_results([])."""
        actions = step.get_actions()
        if actions:
            self.ctx.pending_agent = _step_pending_key(step)
            self.ctx.save(self.context_path)
            exit_with_actions(actions)
        # Inline step
        trigger = step.handle_results([])
        if trigger == _REDISPATCH:
            # After inline results, need another dispatch
            actions2 = step.get_actions()
            if not actions2:
                raise RuntimeError(
                    f"Inline step {type(step).__name__} returned {_REDISPATCH!r} but "
                    "get_actions() still returns [] — infinite loop guard triggered."
                )
            self.ctx.pending_agent = _step_pending_key(step)
            self.ctx.save(self.context_path)
            exit_with_actions(actions2)
        return trigger

    def run(self) -> None:
        if self.machine.state == self.workflow.initial_state:
            boot_trigger = next(iter(self.workflow.transitions[self.workflow.initial_state]))
            self.machine.transition(boot_trigger)
            self.ctx.state = self.machine.state
            self.ctx.save(self.context_path)

        while self.machine.state not in self.workflow.terminal_states:
            step = self.step_handlers.get(self.machine.state)
            if step is None:
                # Unknown state — trigger troubleshooter
                self.ctx.save(self.context_path)
                exit_with_actions([{
                    "action": "spawn_agent",
                    "message": "Pipeline entered an unknown state. Troubleshooter is intervening.",
                    "skill": "troubleshooter",
                    "trigger": "unknown_state",
                    "context_file": str(self.context_path),
                    "cycle_count": 0,
                }])

            current_state = self.machine.state
            trigger = self._dispatch_step(step)

            _apply_counter_updates(self.ctx, current_state, trigger)

            # Check trigger-based troubleshooter conditions
            if self.ctx.signoff_cycle_count >= SIGNOFF_DEADLOCK_THRESHOLD:
                self.ctx.save(self.context_path)
                exit_with_actions([_troubleshooter_descriptor(
                    "signoff_deadlock", self.context_path, self.ctx
                )])

            if self.ctx.review_cycle_count >= REVIEW_LOOP_THRESHOLD:
                self.ctx.save(self.context_path)
                exit_with_actions([_troubleshooter_descriptor(
                    "review_loop", self.context_path, self.ctx
                )])

            self.machine.transition(trigger)
            self.ctx.state = self.machine.state
            self.ctx.save(self.context_path)

        if self.machine.state == "done":
            exit_with_actions([{
                "action": "done",
                "result": "success",
                "reason": f"Pipeline completed for {self.ctx.work_item_id}",
            }])
        else:
            exit_with_actions([{
                "action": "done",
                "result": "failed",
                "reason": f"Pipeline ended in state '{self.machine.state}' for {self.ctx.work_item_id}",
            }])


def _step_pending_key(step: Step) -> str:
    """Return the pending_agent key for a step, falling back to handles."""
    if hasattr(step, "_PENDING_KEY"):
        return step._PENDING_KEY  # type: ignore[attr-defined]
    if hasattr(step, "handles"):
        return step.handles
    return ""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _find_repo_root() -> Path:
    """Walk up from cwd until a directory containing .git or .claude/ is found."""
    current = Path(os.getcwd()).resolve()
    while True:
        if (current / ".claude").is_dir() or (current / ".git").is_dir():
            return current
        parent = current.parent
        if parent == current:
            raise RuntimeError(
                f"Could not locate repo root: no .claude/ or .git directory found "
                f"in any ancestor of {Path(os.getcwd()).resolve()}"
            )
        current = parent


REPO_ROOT = _find_repo_root()

# Resolved after argument parsing; default to the directory containing this script.
PLUGIN_ROOT: Path = Path(__file__).resolve().parent.parent


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (metadata_dict, body)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break

    if end is None:
        return {}, text

    frontmatter_lines = lines[1:end]
    body = "\n".join(lines[end + 1:]).lstrip("\n")

    metadata: dict = {}
    i = 0
    while i < len(frontmatter_lines):
        line = frontmatter_lines[i]
        if ":" in line and not line.startswith(" ") and not line.startswith("-"):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if not value:
                items: list[str] = []
                j = i + 1
                while j < len(frontmatter_lines):
                    item_line = frontmatter_lines[j].strip()
                    if item_line.startswith("- "):
                        items.append(item_line[2:].strip())
                        j += 1
                    else:
                        break
                if items:
                    metadata[key] = items
                    i = j
                    continue
            metadata[key] = value
        i += 1

    return metadata, body




def find_spec_file(work_item_id: str) -> Path:
    """Find the unique _spec_*.md file that contains the work item ID."""
    candidates = [
        p
        for p in REPO_ROOT.rglob("_spec_*.md")
        if ".git" not in p.parts
    ]

    matches = [
        p for p in candidates
        if work_item_id in p.read_text(encoding="utf-8")
    ]

    if not matches:
        print(
            f"Error: no _spec_*.md file found containing '{work_item_id}'.\n"
            f"Verify the task key is correct and you are on the right branch.",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(matches) > 1:
        paths = "\n  ".join(str(m.relative_to(REPO_ROOT)) for m in matches)
        print(
            f"Error: multiple spec files found containing '{work_item_id}' — "
            f"cannot determine which to use:\n  {paths}\n"
            f"Resolve the ambiguity (e.g. deduplicate the task key) and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    return matches[0]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dev_team.py",
        description="dev-team pipeline step machine",
    )
    parser.add_argument("work_item_id", metavar="work-item-id",
                        help="Work item ID (e.g. ADR-172 or Issue-444)")
    parser.add_argument("--workflow", metavar="path", default=None,
                        help="Path to a Mermaid stateDiagram-v2 workflow file")
    parser.add_argument("--research-skill", metavar="skill", default=None,
                        help="Researcher skill to use (e.g. researcher-plan or researcher-issue)")
    parser.add_argument("--plugin-root", metavar="path", default=None,
                        help="Plugin installation root (agents/ and commands/ resolved here)")
    parser.add_argument("--context-file", metavar="path", default=None,
                        help="Path to the pipeline context file (computed by dev-team.md)")
    parser.add_argument("--print-context-path", metavar="repo-slug", default=None,
                        help="Print the context file path for the given repo slug and exit")
    parser.add_argument("--results", metavar="results", default=None,
                        help="Comma-separated one-line results from the previous parallel dispatch")
    args = parser.parse_args()

    # --print-context-path mode: compute and print the context file path, then exit.
    if args.print_context_path is not None:
        print(compute_context_path(args.work_item_id, args.print_context_path), flush=True)
        sys.exit(0)

    # Normal pipeline mode requires --workflow, --research-skill, and --context-file.
    if not args.workflow:
        parser.error("--workflow is required")
    if not args.research_skill:
        parser.error("--research-skill is required")
    if not args.context_file:
        parser.error("--context-file is required")

    global PLUGIN_ROOT
    if args.plugin_root:
        PLUGIN_ROOT = Path(args.plugin_root).resolve()

    work_item_id = args.work_item_id
    workflow_path = Path(args.workflow)
    if not workflow_path.is_absolute():
        workflow_path = REPO_ROOT / workflow_path

    try:
        workflow = parse_workflow(workflow_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error loading workflow: {e}", file=sys.stderr)
        sys.exit(1)

    context_path = Path(args.context_file)
    log_dir = context_path.parent / "logs"

    if context_path.exists():
        ctx = PipelineContext.load(context_path)
        if ctx.state in workflow.terminal_states:
            print(f"Previous run ended with state '{ctx.state}'.")
            print(f"Delete {context_path} to run again.")
            exit_with_actions([{
                "action": "done",
                "result": "success" if ctx.state == "done" else "failed",
                "reason": f"Pipeline previously ended in state '{ctx.state}'",
            }])
        print(f"Resuming {work_item_id} from state '{ctx.state}'...", flush=True)
    else:
        ctx = PipelineContext(work_item_id=work_item_id, state=workflow.initial_state)
        ctx.save(context_path)

    results: list[str] | None = None
    if args.results is not None:
        results = [r.strip() for r in args.results.split(",")]

    DevTeamPipeline(
        ctx, context_path, log_dir, workflow,
        research_skill=args.research_skill,
        results=results,
    ).run()


if __name__ == "__main__":
    main()
