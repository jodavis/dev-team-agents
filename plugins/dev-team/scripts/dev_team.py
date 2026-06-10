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

    The task-runner writes the one-line result indicator from result_format
    ("validated" | "failed") to the Signoff Research section.  Fall back to
    JSON-array parsing for output written by older skill versions.
    """
    text = content.strip()
    if text == "validated":
        return True
    if text == "failed":
        return False
    # Legacy / raw skill output: look for a JSON array with fail/partial items.
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    candidate = fenced.group(1).strip() if fenced else text
    try:
        data = json.loads(candidate)
        if isinstance(data, list):
            return not any(item.get("status") in ("fail", "partial") for item in data)
    except (json.JSONDecodeError, TypeError):
        pass
    # Unrecognised format — treat as not validated so signoff retries.
    return False


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
    def run(self, ctx: PipelineContext) -> str:
        """Execute step logic. Returns a trigger name, OR calls exit_with_actions."""
        ...


class FindSpecStep(Step):
    handles = "spec-finding"

    def run(self, ctx: PipelineContext) -> str:
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

    def __init__(self, context_path: Path) -> None:
        self._context_path = context_path

    def run(self, ctx: PipelineContext) -> str:
        if ctx.debug_report:
            _handle_agent_success(ctx)
            if "# Debug report for" not in ctx.debug_report:
                ctx.last_failure = f"Bug could not be reproduced.\n\n{ctx.debug_report}"
                return "reproduction_failed"
            print("Debugging complete.", flush=True)
            return "debug_done"

        if ctx.pending_agent == self._PENDING_KEY:
            _handle_agent_failure(ctx)
            _check_and_trigger_troubleshooter(
                "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                ctx.consecutive_failures, ctx, self._context_path,
            )

        print(f"Debugger is investigating {ctx.work_item_id}...", flush=True)
        ctx.pending_agent = self._PENDING_KEY
        ctx.save(self._context_path)
        exit_with_actions([{
            "action": "spawn_agent",
            "message": f"Debugger is investigating {ctx.work_item_id}.",
            "agent": "debugger",
            "skill": "debugger-investigate",
            "context_file": str(self._context_path),
            "args": ctx.work_item_id,
            "read_sections": [],
            "write_section": "Debug Report",
            "result_format": "reproduced | not_reproduced",
        }])


class ResearchStep(Step):
    handles = "researching"

    _PENDING_KEY = "research"

    def __init__(self, skill: str, context_path: Path) -> None:
        self._skill = skill
        self._context_path = context_path

    def run(self, ctx: PipelineContext) -> str:
        if ctx.brief:
            _handle_agent_success(ctx)
            print("Research complete.", flush=True)
            return "research_done"

        if ctx.pending_agent == self._PENDING_KEY:
            _handle_agent_failure(ctx)
            _check_and_trigger_troubleshooter(
                "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                ctx.consecutive_failures, ctx, self._context_path,
            )

        print(f"Researcher is planning work for {ctx.work_item_id}...", flush=True)
        read_sections = ["Debug Report"] if ctx.debug_report else []
        ctx.pending_agent = self._PENDING_KEY
        ctx.save(self._context_path)
        exit_with_actions([{
            "action": "spawn_agent",
            "message": f"Researcher is planning work for {ctx.work_item_id}.",
            "agent": "researcher",
            "skill": self._skill,
            "context_file": str(self._context_path),
            "args": f"{ctx.work_item_id} {ctx.spec_path}",
            "read_sections": read_sections,
            "write_section": "Researcher Brief",
            "result_format": "briefed | failed",
        }])


class ImplementStep(Step):
    handles = "implementing"

    _PENDING_KEY = "implement"

    def __init__(self, context_path: Path) -> None:
        self._context_path = context_path

    def run(self, ctx: PipelineContext) -> str:
        if ctx.work_summaries:
            _handle_agent_success(ctx)
            print("Implementation already complete in context — skipping.", flush=True)
            return "impl_done"

        if ctx.pending_agent == self._PENDING_KEY:
            _handle_agent_failure(ctx)
            _check_and_trigger_troubleshooter(
                "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                ctx.consecutive_failures, ctx, self._context_path,
            )

        print(f"Developer is implementing {ctx.work_item_id}...", flush=True)
        ctx.pending_agent = self._PENDING_KEY
        ctx.save(self._context_path)
        exit_with_actions([{
            "action": "spawn_agent",
            "message": "Researcher has written the task brief. Developer is now implementing.",
            "agent": "developer",
            "skill": "developer-implement",
            "context_file": str(self._context_path),
            "read_sections": ["Researcher Brief"],
            "write_section": "Implementation Summary",
            "result_format": "implemented | failed | needs_clarification",
        }])


class ValidateStep(Step):
    handles = "validating"

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir

    def run(self, ctx: PipelineContext) -> str:
        print("Running scripts/validate-build...", flush=True)
        try:
            build_ok, build_log, build_tail = run_validate_script("validate-build", self._log_dir)
        except (FileNotFoundError, OSError) as e:
            print(f"Error running validate-build:\n{e}", file=sys.stderr)
            sys.exit(1)

        ctx.build_log = str(build_log)
        if not build_ok:
            print(f"Build FAILED. Log: {build_log}", flush=True)
            ctx.last_failure = (
                f"Build failed.\n\n"
                f"Full log (read this for details): {build_log}\n\n"
                f"Last 30 lines:\n```\n{build_tail}\n```"
            )
            return "build_failed"

        print(f"Build clean. Log: {build_log}", flush=True)
        print("Running scripts/validate-tests...", flush=True)
        try:
            tests_ok, tests_log, tests_tail = run_validate_script("validate-tests", self._log_dir)
        except (FileNotFoundError, OSError) as e:
            print(f"Error running validate-tests:\n{e}", file=sys.stderr)
            sys.exit(1)

        ctx.test_log = str(tests_log)
        if not tests_ok:
            print(f"Tests FAILED. Log: {tests_log}", flush=True)
            ctx.last_failure = (
                f"Test failures.\n\n"
                f"Full log (read this for details): {tests_log}\n\n"
                f"Last 30 lines:\n```\n{tests_tail}\n```"
            )
            return "tests_failed"

        print(f"Tests clean. Log: {tests_log}", flush=True)
        ctx.last_failure = ""
        _commit_and_push(ctx.work_item_id)
        return "clean"


class ReviewStep(Step):
    handles = "reviewing"

    _PENDING_CREATE_PR = "create-pr"
    _PENDING_REVIEW = "reviewer-review"

    def __init__(self, context_path: Path) -> None:
        self._context_path = context_path

    def run(self, ctx: PipelineContext) -> str:
        # Sub-step 1: create PR
        if not ctx.pr_url:
            if ctx.pending_agent == self._PENDING_CREATE_PR:
                # Re-entry: try to extract pr_url written to the "PR URL" section.
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
                    # Agent ran but pr_url still not populated — treat as failure.
                    _handle_agent_failure(ctx)
                    _check_and_trigger_troubleshooter(
                        "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                        ctx.consecutive_failures, ctx, self._context_path,
                    )

        if not ctx.pr_url:
            print(f"Developer is creating PR for {ctx.work_item_id}...", flush=True)
            read_sections = ["Researcher Brief", "Implementation Summary"]
            for i in range(1, len(ctx.work_summaries)):
                read_sections.append(f"Fix {i}")
            ctx.pending_agent = self._PENDING_CREATE_PR
            ctx.save(self._context_path)
            exit_with_actions([{
                "action": "spawn_agent",
                "message": "Implementation complete. Developer is creating a pull request.",
                "agent": "developer",
                "skill": "developer-create-pr",
                "context_file": str(self._context_path),
                "read_sections": read_sections,
                "write_section": "PR URL",
                "result_format": "pr_created | failed",
            }])

        # Sub-step 2: review
        if ctx.review_notes:
            _handle_agent_success(ctx)
            result = parse_json_output(ctx.review_notes)
            status = result.get("status", "changes_requested")
            if status == "approved":
                print("Review approved.", flush=True)
                return "approved"
            print("Reviewer requested changes.", flush=True)
            return "changes_requested"

        if ctx.pending_agent == self._PENDING_REVIEW:
            _handle_agent_failure(ctx)
            _check_and_trigger_troubleshooter(
                "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                ctx.consecutive_failures, ctx, self._context_path,
            )

        print(f"Reviewer is reviewing {ctx.work_item_id}...", flush=True)
        ctx.pending_agent = self._PENDING_REVIEW
        ctx.save(self._context_path)
        exit_with_actions([{
            "action": "spawn_agent",
            "message": "Pull request created. Reviewer is reviewing the changes.",
            "agent": "reviewer",
            "skill": "reviewer-review",
            "context_file": str(self._context_path),
            "read_sections": ["Researcher Brief"],
            "write_section": "Review Notes",
            "result_format": "approved | changes_requested",
        }])


class SignoffStep(Step):
    handles = "signoff"

    _PENDING_REVIEWER = "signoff-reviewer"
    _PENDING_RESEARCHER = "signoff-researcher"
    _PENDING_PARALLEL = "signoff-parallel"

    def __init__(self, context_path: Path, log_dir: Path) -> None:
        self._context_path = context_path
        self._log_dir = log_dir

    def _make_run_script_descriptor(self, ctx: PipelineContext) -> dict:
        """Build a run_script descriptor for validate-build + validate-tests."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        log_path = self._log_dir / f"{ctx.work_item_id}-signoff-{timestamp}.log"
        ctx.build_log = str(log_path)

        scripts_dir = REPO_ROOT / "scripts"
        ext = ".cmd" if sys.platform == "win32" else ".sh"
        build_script = scripts_dir / f"validate-build{ext}"
        tests_script = scripts_dir / f"validate-tests{ext}"
        if sys.platform == "win32":
            command = f'cmd /c "{build_script}" && cmd /c "{tests_script}"'
        else:
            command = f'bash "{build_script}" && bash "{tests_script}"'

        return {
            "action": "run_script",
            "command": command,
            "log_file": str(log_path),
            "write_section": "Signoff Build Result",
            "result_format": "passed | failed",
        }

    def run(self, ctx: PipelineContext) -> str:
        # Push first so the reviewer can see the latest commits.
        _commit_and_push(ctx.work_item_id)

        # Sub-step 1, 2 & 3: spawn reviewer, researcher, and build/test script in parallel.
        if not ctx.signoff_review and not ctx.signoff_research:
            if ctx.pending_agent in (self._PENDING_REVIEWER, self._PENDING_RESEARCHER,
                                     self._PENDING_PARALLEL):
                # Re-entry after parallel spawn with no results — treat as failure.
                _handle_agent_failure(ctx)
                _check_and_trigger_troubleshooter(
                    "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                    ctx.consecutive_failures, ctx, self._context_path,
                )

            print(f"Spawning reviewer, researcher, and build/test in parallel for "
                  f"{ctx.work_item_id}...", flush=True)
            read_sections_researcher = ["Researcher Brief", "Implementation Summary"]
            for i in range(1, len(ctx.work_summaries)):
                read_sections_researcher.append(f"Fix {i}")
            run_script_desc = self._make_run_script_descriptor(ctx)
            ctx.pending_agent = self._PENDING_PARALLEL
            ctx.save(self._context_path)
            exit_with_actions([
                {
                    "action": "spawn_agent",
                    "agent": "task-runner",
                    "skill": "reviewer-sign-off",
                    "context_file": str(self._context_path),
                    "read_sections": ["Researcher Brief"],
                    "write_section": "Signoff Review",
                    "result_format": "approved | changes_requested",
                },
                {
                    "action": "spawn_agent",
                    "agent": "task-runner",
                    "skill": "researcher-validate",
                    "context_file": str(self._context_path),
                    "read_sections": read_sections_researcher,
                    "write_section": "Signoff Research",
                    "result_format": "validated | failed",
                },
                run_script_desc,
            ])

        # Sub-step 1: reviewer sign-off (sequential fallback: only reviewer missing)
        if not ctx.signoff_review:
            if ctx.pending_agent == self._PENDING_REVIEWER:
                _handle_agent_failure(ctx)
                _check_and_trigger_troubleshooter(
                    "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                    ctx.consecutive_failures, ctx, self._context_path,
                )

            print(f"Reviewer is signing off {ctx.work_item_id}...", flush=True)
            ctx.pending_agent = self._PENDING_REVIEWER
            ctx.save(self._context_path)
            exit_with_actions([{
                "action": "spawn_agent",
                "message": "Researcher validated. Reviewer is performing final sign-off.",
                "agent": "task-runner",
                "skill": "reviewer-sign-off",
                "context_file": str(self._context_path),
                "read_sections": ["Researcher Brief"],
                "write_section": "Signoff Review",
                "result_format": "approved | changes_requested",
            }])

        # signoff_review is populated — reviewer agent succeeded
        _handle_agent_success(ctx)

        # Sub-step 2: researcher validate (sequential fallback: only researcher missing)
        if not ctx.signoff_research:
            if ctx.pending_agent == self._PENDING_RESEARCHER:
                _handle_agent_failure(ctx)
                _check_and_trigger_troubleshooter(
                    "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                    ctx.consecutive_failures, ctx, self._context_path,
                )

            print(f"Researcher is validating {ctx.work_item_id}...", flush=True)
            read_sections = ["Researcher Brief", "Implementation Summary"]
            for i in range(1, len(ctx.work_summaries)):
                read_sections.append(f"Fix {i}")
            ctx.pending_agent = self._PENDING_RESEARCHER
            ctx.save(self._context_path)
            exit_with_actions([{
                "action": "spawn_agent",
                "message": "Reviewer signed off. Researcher is validating exit criteria.",
                "agent": "task-runner",
                "skill": "researcher-validate",
                "context_file": str(self._context_path),
                "read_sections": read_sections,
                "write_section": "Signoff Research",
                "result_format": "validated | failed",
            }])

        # Both review and research are populated — both agents succeeded
        _handle_agent_success(ctx)

        # Sub-step 3: build/test script (sequential fallback: build result missing)
        if not ctx.signoff_build_result:
            pending_key = "signoff-build"
            if ctx.pending_agent == pending_key:
                _handle_agent_failure(ctx)
                _check_and_trigger_troubleshooter(
                    "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                    ctx.consecutive_failures, ctx, self._context_path,
                )

            print(f"Running build/test validation for {ctx.work_item_id}...", flush=True)
            run_script_desc = self._make_run_script_descriptor(ctx)
            ctx.pending_agent = pending_key
            ctx.save(self._context_path)
            exit_with_actions([run_script_desc])

        # All three results available — process them
        failures: list[str] = []

        build_passed = ctx.signoff_build_result.strip().startswith("passed")
        if not build_passed:
            failures.append(
                f"Build/test validation failed. Log: {ctx.build_log}\n"
                f"Script result: {ctx.signoff_build_result.strip()}"
            )

        reviewer_result = parse_json_output(ctx.signoff_review)
        reviewer_approved = reviewer_result.get("status", "changes_requested") == "approved"
        if not reviewer_approved:
            failures.append(f"Reviewer sign-off:\n{ctx.signoff_review}")

        researcher_ok = _researcher_validated(ctx.signoff_research)
        if not researcher_ok:
            failures.append(f"Research validation:\n{ctx.signoff_research}")

        # Reset sub-step sections for the next signoff cycle
        ctx.signoff_review = ""
        ctx.signoff_research = ""
        ctx.signoff_build_result = ""
        ctx.pending_agent = ""

        if failures:
            ctx.review_notes = "\n\n---\n\n".join(failures)
            ctx.last_failure = ctx.review_notes
            print("Signoff found issues; requesting further changes.", flush=True)
            return "changes_requested"

        ctx.last_failure = ""
        print("Signoff approved.", flush=True)
        return "approved"


class FixStep(Step):
    handles = "fixing"

    def __init__(self, context_path: Path) -> None:
        self._context_path = context_path

    def run(self, ctx: PipelineContext) -> str:
        # Total completed fix summaries before this step runs
        completed = 1 + ctx.fix_iteration + ctx.review_fix_iteration
        pending_key = f"fix-{completed}"

        if len(ctx.work_summaries) > completed:
            # Fix agent wrote a new summary since last iteration
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

        if ctx.pending_agent == pending_key:
            _handle_agent_failure(ctx)
            _check_and_trigger_troubleshooter(
                "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                ctx.consecutive_failures, ctx, self._context_path,
            )

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

        ctx.pending_agent = pending_key
        ctx.save(self._context_path)
        exit_with_actions([{
            "action": "spawn_agent",
            "message": (
                f"Build or tests failed. Developer is fixing "
                f"(iteration {ctx.fix_iteration + 1} of {MAX_FIX_ITERATIONS})."
            ),
            "agent": "developer",
            "skill": "developer-fix",
            "context_file": str(self._context_path),
            "read_sections": read_sections,
            "write_section": write_section,
            "result_format": "fixed | failed",
        }])


class FixPrStep(Step):
    handles = "fixing-pr"

    def __init__(self, context_path: Path) -> None:
        self._context_path = context_path

    def run(self, ctx: PipelineContext) -> str:
        completed = 1 + ctx.fix_iteration + ctx.review_fix_iteration
        pending_key = f"fix-pr-{completed}"

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

        if ctx.pending_agent == pending_key:
            _handle_agent_failure(ctx)
            _check_and_trigger_troubleshooter(
                "consecutive_failures", CONSECUTIVE_FAILURES_THRESHOLD,
                ctx.consecutive_failures, ctx, self._context_path,
            )

        write_section = f"Fix {completed}"
        print(
            f"Invoking developer to address review comments "
            f"(iteration {ctx.review_fix_iteration + 1} of {MAX_REVIEW_FIX_ITERATIONS})...",
            flush=True,
        )
        read_sections = ["Researcher Brief", "Review Notes", "Implementation Summary"]
        for i in range(1, len(ctx.work_summaries)):
            read_sections.append(f"Fix {i}")

        # When a PR exists, include failing GitHub Actions check output in the fix context
        # instead of running validate scripts in-process.
        if ctx.pr_url:
            pr_checks_output = _get_failing_pr_checks(ctx.pr_url)
            if pr_checks_output:
                ctx.last_failure = (
                    f"{ctx.review_notes}\n\n"
                    f"Failing GitHub Actions checks:\n```\n{pr_checks_output}\n```"
                )

        ctx.pending_agent = pending_key
        ctx.save(self._context_path)
        exit_with_actions([{
            "action": "spawn_agent",
            "message": (
                f"Review requested changes. Developer is addressing review comments "
                f"(iteration {ctx.review_fix_iteration + 1} of {MAX_REVIEW_FIX_ITERATIONS})."
            ),
            "agent": "developer",
            "skill": "developer-fix",
            "context_file": str(self._context_path),
            "read_sections": read_sections,
            "write_section": write_section,
            "result_format": "fixed | failed",
        }])


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
    ) -> None:
        self.ctx = ctx
        self.context_path = context_path
        self.log_dir = log_dir
        self.workflow = workflow
        self.machine = StateMachine(workflow.transitions, initial=ctx.state)
        self.step_handlers: dict[str, Step] = {
            "spec-finding": FindSpecStep(),
            "debugging": DebugStep(context_path),
            "researching": ResearchStep(research_skill, context_path),
            "implementing": ImplementStep(context_path),
            "validating": ValidateStep(log_dir),
            "fixing": FixStep(context_path),
            "reviewing": ReviewStep(context_path),
            "signoff": SignoffStep(context_path, log_dir),
            "fixing-pr": FixPrStep(context_path),
        }

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
            trigger = step.run(self.ctx)

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


def run_validate_script(script_name: str, log_dir: Path) -> tuple[bool, Path, str]:
    """Run a validate script from the scripts/ directory.

    Logs full output to a timestamped file under log_dir.
    Returns (success, log_path, tail) where tail is the last 30 lines.
    """
    scripts_dir = REPO_ROOT / "scripts"
    ext = ".cmd" if sys.platform == "win32" else ".sh"
    script_path = scripts_dir / f"{script_name}{ext}"

    if not script_path.exists():
        raise FileNotFoundError(
            f"Validate script not found: scripts/{script_name}{ext}"
        )

    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"{timestamp}-{script_name}.txt"

    if sys.platform == "win32":
        invoke = ["cmd", "/c", str(script_path)]
    else:
        invoke = [str(script_path)]

    proc = subprocess.Popen(
        invoke,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
    )

    lines: list[str] = []
    with log_path.open("w", encoding="utf-8") as log_file:
        for line in proc.stdout:  # type: ignore[union-attr]
            log_file.write(line)
            lines.append(line)

    proc.wait()
    tail = "".join(lines[-30:])
    return proc.returncode == 0, log_path, tail


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

    DevTeamPipeline(ctx, context_path, log_dir, workflow, research_skill=args.research_skill).run()


if __name__ == "__main__":
    main()
