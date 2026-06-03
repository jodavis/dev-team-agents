#!/usr/bin/env python3
"""dev-team pipeline orchestrator.

Entry point: main() — accepts a Jira work item ID, finds the matching spec file,
and runs the dev-team pipeline. Reentrant: if a context file exists from a prior
interrupted run, execution resumes from the last completed state.

To start fresh, delete the context file:
  .claude/logs/dev-team/<work-item-id>-context.md
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

# Reconfigure stdout/stderr to UTF-8 early so that Unicode characters in agent
# output (e.g. arrows, bullets) don't crash on Windows cp1252 terminals.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

MAX_FIX_ITERATIONS = 5
MAX_REVIEW_FIX_ITERATIONS = 3


# ---------------------------------------------------------------------------
# Workflow definition (parsed from a Mermaid stateDiagram-v2 file)
# ---------------------------------------------------------------------------

@dataclass
class WorkflowDefinition:
    transitions: dict[str, dict[str, str]]
    terminal_states: set[str]
    initial_state: str


@dataclass
class ReviewComment:
    author: str   # "Reviewer" or "Developer"
    comment: str


@dataclass
class ReviewThread:
    id: str           # 8-char UUID assigned by dev_team.py
    file_path: str    # repo-relative path
    line_number: int  # 1-based
    resolved: bool    # set only by the Reviewer
    comments: list[ReviewComment] = field(default_factory=list)


def parse_workflow(path: Path) -> WorkflowDefinition:
    """Parse a Mermaid stateDiagram-v2 block from a markdown file.

    Recognises three line forms inside the diagram:
      [*] --> StateA          → initial state
      StateA --> [*]          → terminal state
      StateA --> StateB : t   → transition with trigger t
    """
    text = path.read_text(encoding="utf-8")

    # Extract the first ```mermaid ... ``` fenced block.
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

        # StateA --> [*]  (terminal)
        m = re.match(r"^([\w-]+)\s+-->\s+\[\*\]$", line)
        if m:
            terminal_states.add(m.group(1))
            continue

        # [*] --> StateA  (initial)
        m = re.match(r"^\[\*\]\s+-->\s+([\w-]+)$", line)
        if m:
            initial_state = m.group(1)
            continue

        # StateA --> StateB : trigger
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
    """All mutable state for a dev-team pipeline run, persisted across resumptions."""

    work_item_id: str
    spec_path: str = ""
    state: str = "init"
    brief: str = ""
    work_summaries: list[str] = field(default_factory=list)
    fix_iteration: int = 0
    review_fix_iteration: int = 0
    base_branch: str = ""
    first_push_done: bool = False
    review_threads: list[ReviewThread] = field(default_factory=list)
    review_notes: str = ""
    last_failure: str = ""
    build_log: str = ""
    test_log: str = ""
    debug_report: str = ""
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
            f"base_branch: {self.base_branch}",
            f"first_push_done: {'true' if self.first_push_done else 'false'}",
            f"build_log: {self.build_log}",
            f"test_log: {self.test_log}",
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

        lines += ["", "<!-- section:Review Notes -->", "", self.review_notes.strip()]

        threads_json = json.dumps(
            [
                {
                    "id": t.id,
                    "filePath": t.file_path,
                    "lineNumber": t.line_number,
                    "resolved": t.resolved,
                    "comments": [
                        {"author": c.author, "comment": c.comment}
                        for c in t.comments
                    ],
                }
                for t in self.review_threads
            ],
            indent=2,
        )
        lines += ["", "<!-- section:Review Threads -->", "", threads_json]

        if self.last_failure:
            lines += ["", "<!-- section:Last Failure -->", "", self.last_failure.strip()]

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
            base_branch=meta.get("base_branch", ""),
            first_push_done=meta.get("first_push_done", "false") == "true",
            build_log=meta.get("build_log", ""),
            test_log=meta.get("test_log", ""),
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

        review_threads_json = sections.get("Review Threads", "").strip()
        if review_threads_json:
            try:
                threads_data = json.loads(review_threads_json)
                ctx.review_threads = [
                    ReviewThread(
                        id=t["id"],
                        file_path=t["filePath"],
                        line_number=t["lineNumber"],
                        resolved=t["resolved"],
                        comments=[
                            ReviewComment(author=c["author"], comment=c["comment"])
                            for c in t.get("comments", [])
                        ],
                    )
                    for t in threads_data
                ]
            except (json.JSONDecodeError, KeyError, TypeError):
                ctx.review_threads = []

        ctx.last_failure = sections.get("Last Failure", "")

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

def _commit_and_push(work_item_id: str) -> None:
    """Push the current branch. The developer is expected to have already committed.

    As a safety net, stages and commits any uncommitted changes left by the developer
    before pushing. The pipeline never pushes until validation is clean, so partial
    or broken work is never sent to the remote.
    """
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
    """Extract the last parseable JSON object from agent output text.

    Tries single-line JSON objects from the end of the text first, then
    fenced code blocks. Returns an empty dict if nothing parses.
    """
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


def parse_json_list_output(text: str) -> list:
    """Extract the last parseable JSON array from agent output text.

    Tries fenced code blocks first (from the end), then bare JSON lines.
    Returns an empty list if nothing parses.
    """
    for block in reversed(re.findall(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)):
        try:
            result = json.loads(block.strip())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            continue

    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            try:
                result = json.loads(line)
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                continue

    return []


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class Step(ABC):
    """A single phase of the dev-team pipeline."""

    handles: str  # state name this step is responsible for

    @abstractmethod
    def run(self, ctx: PipelineContext) -> str:
        """Execute step logic (or skip if already done). Returns a trigger name."""
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

    def run(self, ctx: PipelineContext) -> str:
        if ctx.debug_report:
            print("Debug report already in context — skipping.", flush=True)
            return "debug_done"
        print(f"Debugger is investigating {ctx.work_item_id}...", flush=True)
        try:
            output = call_agent("debugger", "debugger-investigate", ctx.work_item_id)
        except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
            print(f"Error invoking debugger agent:\n{e}", file=sys.stderr)
            sys.exit(1)

        result = parse_json_output(output)
        status = result.get("status", "reproduced")

        if status == "not_reproduced":
            reason = result.get("reason", "No reason given.")
            ctx.last_failure = f"Bug could not be reproduced.\n\n{reason}"
            print(f"Reproduction failed: {reason}", flush=True)
            return "reproduction_failed"

        marker = "# Debug report for"
        marker_pos = output.find(marker)
        if marker_pos == -1:
            print(
                f"Error: debugger did not return a valid report. "
                f"Expected output containing '# Debug report for'.\n\n"
                f"Debugger output:\n{output}",
                file=sys.stderr,
            )
            sys.exit(1)
        ctx.debug_report = output[marker_pos:]
        print("Debugging complete.", flush=True)
        return "debug_done"


class ResearchStep(Step):
    handles = "researching"

    def __init__(self, skill: str) -> None:
        self._skill = skill

    def run(self, ctx: PipelineContext) -> str:
        if ctx.brief:
            print("Research already complete in context — skipping.", flush=True)
            return "research_done"
        print(f"Researcher is planning work for {ctx.work_item_id}...", flush=True)
        try:
            brief = call_agent(
                "researcher", self._skill,
                ctx.work_item_id, ctx.spec_path,
                substitutions={"$DEBUG_REPORT": ctx.debug_report},
            )
        except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
            print(f"Error invoking researcher agent:\n{e}", file=sys.stderr)
            sys.exit(1)
        marker = "# Implementation plan for"
        marker_pos = brief.find(marker)
        if marker_pos == -1:
            print(
                f"Error: researcher did not return a valid task brief. "
                f"Expected output containing '# Implementation plan for'.\n\n"
                f"Researcher output:\n{brief}",
                file=sys.stderr,
            )
            sys.exit(1)
        ctx.brief = brief[marker_pos:]
        return "research_done"


class ImplementStep(Step):
    handles = "implementing"

    def run(self, ctx: PipelineContext) -> str:
        if ctx.work_summaries:
            print("Implementation already complete in context — skipping.", flush=True)
            return "impl_done"
        print(f"Developer is implementing {ctx.work_item_id}...", flush=True)
        try:
            impl_summary = call_agent(
                "developer", "developer-implement", ctx.work_item_id,
                substitutions={"$TASK_BRIEF": ctx.brief},
            )
        except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
            print(f"Error invoking developer agent:\n{e}", file=sys.stderr)
            sys.exit(1)
        ctx.work_summaries.append(impl_summary)
        return "impl_done"


class ValidateStep(Step):
    handles = "validating"

    def run(self, ctx: PipelineContext) -> str:
        print("Running scripts/validate-build...", flush=True)
        try:
            build_ok, build_log, build_tail = run_validate_script("validate-build")
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
            tests_ok, tests_log, tests_tail = run_validate_script("validate-tests")
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

    def __init__(self, context_path: Path) -> None:
        self._context_path = context_path

    def run(self, ctx: PipelineContext) -> str:
        if not ctx.pr_url:
            print(f"Developer is creating PR for {ctx.work_item_id}...", flush=True)
            try:
                pr_output = call_agent(
                    "developer", "developer-create-pr",
                    substitutions={
                        "$WORK_ITEM_ID": ctx.work_item_id,
                        "$PR_URL": ctx.pr_url,
                        "$TASK_BRIEF": ctx.brief,
                        "$WORK_SUMMARIES": _format_work_summaries(ctx.work_summaries),
                    },
                )
            except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
                print(f"Error invoking developer-create-pr agent:\n{e}", file=sys.stderr)
                sys.exit(1)
            pr_result = parse_json_output(pr_output)
            if pr_result.get("pr_url"):
                ctx.pr_url = pr_result["pr_url"]
                ctx.save(self._context_path)
            else:
                print("Error: developer-create-pr did not return a pr_url.", file=sys.stderr)
                sys.exit(1)

        print(f"Reviewer is reviewing {ctx.work_item_id}...", flush=True)
        try:
            output = call_agent(
                "reviewer", "reviewer-review",
                substitutions={
                    "$WORK_ITEM_ID": ctx.work_item_id,
                    "$SPEC_PATH": ctx.spec_path,
                    "$TASK_BRIEF": ctx.brief,
                    "$PR_URL": ctx.pr_url,
                },
            )
        except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
            print(f"Error invoking reviewer agent:\n{e}", file=sys.stderr)
            sys.exit(1)

        result = parse_json_output(output)
        if result.get("pr_url"):
            ctx.pr_url = result["pr_url"]
        ctx.review_notes = output

        status = result.get("status", "changes_requested")
        if status == "approved":
            print("Review approved.", flush=True)
            return "approved"
        print("Reviewer requested changes.", flush=True)
        return "changes_requested"


class SignoffStep(Step):
    handles = "signoff"

    def run(self, ctx: PipelineContext) -> str:
        print(f"Running parallel signoff for {ctx.work_item_id}...", flush=True)

        # Push first so the reviewer-sign-off agent can see the latest commits on the PR.
        _commit_and_push(ctx.work_item_id)

        failures: list[str] = []

        def _run_scripts() -> tuple[bool, str]:
            try:
                build_ok, build_log, build_tail = run_validate_script("validate-build")
            except (FileNotFoundError, OSError) as e:
                return False, f"validate-build error: {e}"
            if not build_ok:
                return False, (
                    f"Build failed.\n\n"
                    f"Full log: {build_log}\n\n"
                    f"Last 30 lines:\n```\n{build_tail}\n```"
                )
            try:
                tests_ok, tests_log, tests_tail = run_validate_script("validate-tests")
            except (FileNotFoundError, OSError) as e:
                return False, f"validate-tests error: {e}"
            if not tests_ok:
                return False, (
                    f"Test failures.\n\n"
                    f"Full log: {tests_log}\n\n"
                    f"Last 30 lines:\n```\n{tests_tail}\n```"
                )
            return True, ""

        def _run_reviewer_signoff() -> tuple[bool, str, str]:
            try:
                output = call_agent(
                    "reviewer", "reviewer-sign-off",
                    substitutions={
                        "$WORK_ITEM_ID": ctx.work_item_id,
                        "$TASK_BRIEF": ctx.brief,
                        "$PR_URL": ctx.pr_url,
                    },
                )
            except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
                return False, "", f"reviewer-sign-off error: {e}"
            result = parse_json_output(output)
            pr_url = result.get("pr_url", "")
            approved = result.get("status", "changes_requested") == "approved"
            return approved, pr_url, output if not approved else ""

        def _run_researcher_validate() -> tuple[bool, str]:
            try:
                output = call_agent(
                    "researcher", "researcher-validate",
                    ctx.work_item_id, ctx.spec_path,
                    substitutions={
                        "$TASK_BRIEF": ctx.brief,
                        "$WORK_SUMMARIES": _format_work_summaries(ctx.work_summaries),
                    },
                )
            except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
                return False, f"researcher-validate error: {e}"

            # Parse the JSON array returned by researcher-validate
            try:
                for block in reversed(re.findall(r"```(?:json)?\s*\n([\s\S]*?)\n```", output)):
                    try:
                        criteria = json.loads(block.strip())
                        if isinstance(criteria, list):
                            failing = [
                                c for c in criteria
                                if c.get("status") in ("fail", "partial")
                            ]
                            if failing:
                                details = "\n".join(
                                    f"- [{c['status'].upper()}] {c['criterion']}: "
                                    f"{c.get('finding', '')}"
                                    for c in failing
                                )
                                return False, f"Exit criteria not fully met:\n{details}"
                            return True, ""
                    except (json.JSONDecodeError, KeyError):
                        continue
            except Exception:
                pass
            return True, ""  # No parseable JSON — treat as inconclusive pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            fut_scripts = executor.submit(_run_scripts)
            fut_reviewer = executor.submit(_run_reviewer_signoff)
            fut_researcher = executor.submit(_run_researcher_validate)

            scripts_ok, scripts_failure = fut_scripts.result()
            reviewer_ok, reviewer_pr_url, reviewer_failure = fut_reviewer.result()
            researcher_ok, researcher_failure = fut_researcher.result()

        if reviewer_pr_url:
            ctx.pr_url = reviewer_pr_url

        if not scripts_ok:
            failures.append(scripts_failure)
        if not reviewer_ok:
            failures.append(f"Reviewer sign-off:\n{reviewer_failure}")
        if not researcher_ok:
            failures.append(researcher_failure)

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

    def run(self, ctx: PipelineContext) -> str:
        if ctx.fix_iteration >= MAX_FIX_ITERATIONS:
            print(
                f"Error: still failing after {MAX_FIX_ITERATIONS} fix iterations. "
                f"Manual intervention needed.",
                file=sys.stderr,
            )
            return "max_retries"
        print(
            f"Invoking developer to fix "
            f"(iteration {ctx.fix_iteration + 1} of {MAX_FIX_ITERATIONS})...",
            flush=True,
        )
        try:
            fix_summary = call_agent(
                "developer", "developer-fix", ctx.work_item_id,
                substitutions={
                    "$TASK_BRIEF": ctx.brief,
                    "$WORK_SUMMARIES": _format_work_summaries(ctx.work_summaries),
                    "$ISSUES": ctx.last_failure,
                },
            )
        except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
            print(f"Error invoking developer-fix agent:\n{e}", file=sys.stderr)
            sys.exit(1)
        ctx.fix_iteration += 1
        ctx.work_summaries.append(fix_summary)
        return "fix_done"


class FixPrStep(Step):
    handles = "fixing-pr"

    def run(self, ctx: PipelineContext) -> str:
        if ctx.review_fix_iteration >= MAX_REVIEW_FIX_ITERATIONS:
            print(
                f"Error: still failing review after {MAX_REVIEW_FIX_ITERATIONS} "
                f"review fix iterations. Manual intervention needed.",
                file=sys.stderr,
            )
            return "max_retries"
        print(
            f"Invoking developer to address review comments "
            f"(iteration {ctx.review_fix_iteration + 1} of {MAX_REVIEW_FIX_ITERATIONS})...",
            flush=True,
        )
        issues = (
            f"Code review changes requested on PR {ctx.pr_url}.\n\n"
            f"Read the open review threads on the PR and address each one.\n\n"
            f"Reviewer summary:\n{ctx.review_notes}"
        )
        try:
            fix_summary = call_agent(
                "developer", "developer-fix", ctx.work_item_id,
                substitutions={
                    "$TASK_BRIEF": ctx.brief,
                    "$WORK_SUMMARIES": _format_work_summaries(ctx.work_summaries),
                    "$ISSUES": issues,
                },
            )
        except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
            print(f"Error invoking developer-fix agent:\n{e}", file=sys.stderr)
            sys.exit(1)
        ctx.review_fix_iteration += 1
        ctx.work_summaries.append(fix_summary)
        return "fix_done"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class DevTeamPipeline:
    """Drives the dev-team state machine from init (or a resumed state) to done."""

    def __init__(self, ctx: PipelineContext, context_path: Path, workflow: WorkflowDefinition, research_skill: str) -> None:
        self.ctx = ctx
        self.context_path = context_path
        self.workflow = workflow
        self.machine = StateMachine(workflow.transitions, initial=ctx.state)
        self.step_handlers: dict[str, Step] = {
            "spec-finding": FindSpecStep(),
            "debugging": DebugStep(),
            "researching": ResearchStep(research_skill),
            "implementing": ImplementStep(),
            "validating": ValidateStep(),
            "fixing": FixStep(),
            "reviewing": ReviewStep(context_path),
            "signoff": SignoffStep(),
            "fixing-pr": FixPrStep(),
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
                raise RuntimeError(f"No handler for state '{self.machine.state}'")

            trigger = step.run(self.ctx)

            self.machine.transition(trigger)
            self.ctx.state = self.machine.state
            self.ctx.save(self.context_path)

        if self.machine.state == "failed":
            sys.exit(1)


# ---------------------------------------------------------------------------
# Utilities (unchanged from original)
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


def _resolve_claude() -> list[str]:
    """Return the command prefix needed to invoke the claude CLI.

    On Windows, claude is often a .cmd batch wrapper. CreateProcess can't run
    .cmd files directly — they need cmd.exe. shutil.which resolves the full
    path (respecting PATHEXT), and we wrap with cmd /c if needed.
    """
    path = shutil.which("claude")
    if path is None:
        raise RuntimeError(
            "claude CLI not found on PATH. "
            "Ensure Claude Code is installed and claude.exe is accessible."
        )
    if sys.platform == "win32" and path.lower().endswith(".cmd"):
        return ["cmd", "/c", path]
    return [path]


CLAUDE_CMD = _resolve_claude()


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (metadata_dict, body).

    Frontmatter is delimited by lines containing only '---'.
    If no frontmatter is present, returns ({}, text).
    """
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


class _MdStreamWriter:
    """Translates stream_event deltas into formatted markdown in real time.

    Handles three content block types:
      - thinking  → block quote (> prefix on every line)
      - text      → plain text, written as-is
      - tool_use  → JSON code block written on block_stop once the full input is accumulated
    All other event types are ignored.
    """

    def __init__(self, file: "IO[str]") -> None:
        self._file = file
        self._block_type: str | None = None
        self._tool_name: str = ""
        self._tool_json: str = ""
        self._at_line_start: bool = True  # tracks position within a thinking block

    @staticmethod
    def _ts() -> str:
        return datetime.datetime.now().strftime("%H:%M:%S")

    def handle(self, event: dict) -> None:
        if event.get("type") != "stream_event":
            return
        se = event.get("event", {})
        se_type = se.get("type")

        if se_type == "content_block_start":
            block = se.get("content_block", {})
            self._block_type = block.get("type")
            self._tool_name = block.get("name", "unknown")
            self._tool_json = ""
            self._at_line_start = True
            if self._block_type == "thinking":
                self._file.write(f"\n**[{self._ts()}]**\n")
                self._file.flush()
            elif self._block_type == "text":
                self._file.write(f"\n**[{self._ts()}]**\n")
                self._file.flush()

        elif se_type == "content_block_delta":
            delta = se.get("delta", {})
            dtype = delta.get("type")
            if dtype == "thinking_delta" and self._block_type == "thinking":
                self._write_thinking(delta.get("thinking", ""))
            elif dtype == "text_delta" and self._block_type == "text":
                self._file.write(delta.get("text", ""))
                self._file.flush()
            elif dtype == "input_json_delta" and self._block_type == "tool_use":
                self._tool_json += delta.get("partial_json", "")

        elif se_type == "content_block_stop":
            if self._block_type == "thinking":
                if not self._at_line_start:
                    self._file.write("\n")
                self._file.write("\n")
                self._file.flush()
            elif self._block_type == "tool_use":
                try:
                    tool_input = json.loads(self._tool_json) if self._tool_json else {}
                except json.JSONDecodeError:
                    tool_input = {"raw": self._tool_json}
                ts = self._ts()
                if self._tool_name == "Bash":
                    self._file.write(
                        f"\n**[{ts}]**\n"
                        f"```bash\n"
                        f"# {tool_input['description']}\n"
                        f"{tool_input['command']}\n"
                        f"```\n\n"
                    )
                elif self._tool_name == "Read":
                    self._file.write(
                        f"\n**[{ts}]**\n"
                        f"```\n"
                        f"Reading {tool_input['file_path']}\n"
                        f"```\n\n"
                    )
                elif self._tool_name == "Glob":
                    self._file.write(
                        f"\n**[{ts}]**\n"
                        f"```\n"
                        f"Searching for {tool_input['pattern']}\n"
                        f"```\n\n"
                    )
                elif self._tool_name == "Grep":
                    self._file.write(
                        f"\n**[{ts}]**\n"
                        f"```\n"
                        f"Searching for {tool_input['pattern']} in {tool_input.get('glob', '')}\n"
                        f"```\n\n"
                    )
                else:
                    self._file.write(
                        f"\n**[{ts}]**\n"
                        f"```json\n"
                        f"{json.dumps({'tool': self._tool_name, 'input': tool_input}, indent=2, ensure_ascii=False)}\n"
                        f"```\n\n"
                    )
                self._file.flush()
            self._block_type = None

    def _write_thinking(self, text: str) -> None:
        for ch in text:
            if self._at_line_start:
                self._file.write("> ")
                self._at_line_start = False
            if ch == "\n":
                self._file.write("\n")
                self._at_line_start = True
            else:
                self._file.write(ch)
        self._file.flush()


def call_agent(
    agent_name: str,
    skill_name: str,
    *args: str,
    stream: bool = True,
    substitutions: dict[str, str] | None = None,
) -> str:
    """Invoke a Claude agent with a skill via the claude CLI.

    Reads the agent definition for its model and system prompt, reads the skill
    definition for its instructions, and calls `claude -p` with the combined prompt.

    Args:
        agent_name:     Name of the agent (matches ../agents/<name>.md).
        skill_name:     Name of the skill (matches ../commands/<name>.md).
        *args:          Arguments passed to the skill, substituted for $ARGUMENTS.
        stream:         If True (default), print output to stdout as it arrives.
                        Set to False for agents that return structured JSON.
        substitutions:  Optional dict of {placeholder: value} pairs substituted into
                        the skill body before $ARGUMENTS is resolved. Use for embedding
                        structured content (e.g. {"$TASK_BRIEF": brief_text}).

    Returns:
        The full text output from the agent.

    Raises:
        FileNotFoundError: Agent or skill definition file not found.
        RuntimeError: claude CLI not on PATH, or unexpected output format.
        subprocess.CalledProcessError: claude CLI exited with non-zero status.
    """
    agent_path = PLUGIN_ROOT / "agents" / f"{agent_name}.md"
    skill_path = PLUGIN_ROOT / "commands" / f"{skill_name}.md"

    if not agent_path.exists():
        raise FileNotFoundError(
            f"Agent definition not found: {agent_path}"
        )
    if not skill_path.exists():
        raise FileNotFoundError(
            f"Skill definition not found: {skill_path}"
        )

    agent_meta, agent_body = _parse_frontmatter(agent_path.read_text(encoding="utf-8"))
    _, skill_body = _parse_frontmatter(skill_path.read_text(encoding="utf-8"))

    if substitutions:
        for placeholder, value in substitutions.items():
            skill_body = skill_body.replace(placeholder, value)
    arguments_str = " ".join(args)
    skill_body = skill_body.replace("$ARGUMENTS", arguments_str)

    prompt = f"{agent_body}\n\n---\n\n{skill_body}"

    raw_model = agent_meta.get("model", "sonnet")
    model = MODEL_MAP.get(raw_model, raw_model)

    # Pass -p without an argument so claude runs in print mode reading from stdin.
    # Embedding the prompt inline as "-p <prompt>" hits the Windows 32 767-char
    # CreateProcess limit once build/test output fills $ISSUES.
    cmd = CLAUDE_CMD + [
        "-p", "--model", model,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages"
    ]
    tools = agent_meta.get("tools")
    if isinstance(tools, list) and tools:
        cmd += ["--allowedTools", ",".join(tools)]

    timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    log_dir = REPO_ROOT / ".claude" / "logs" / "dev-team"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{timestamp}-{agent_name}-{skill_name}.jsonl"
    md_log_path = log_dir / f"{timestamp}-{agent_name}-{skill_name}.md"

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=REPO_ROOT,
        )
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(
            f"Failed to start claude CLI: {exc}"
        ) from exc

    # Write the prompt to stdin in a background thread.  Without the thread the
    # pipe buffer fills up before we start reading stdout, causing a deadlock.
    def _write_prompt() -> None:
        try:
            proc.stdin.write(prompt)  # type: ignore[union-attr]
            proc.stdin.close()        # type: ignore[union-attr]
        except BrokenPipeError:
            pass

    stdin_thread = threading.Thread(target=_write_prompt, daemon=True)
    stdin_thread.start()

    result_text: str = ""
    started_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("w", encoding="utf-8") as log_file, \
         md_log_path.open("w", encoding="utf-8") as md_file:
        md_file.write(f"# {agent_name} / {skill_name}\n\nStarted: {started_ts}\n\n")
        md_file.flush()
        md_writer = _MdStreamWriter(md_file)
        for line in proc.stdout:  # type: ignore[union-attr]
            log_file.write(line)
            log_file.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            md_writer.handle(event)
            if event.get("type") == "result" and event.get("subtype") == "success":
                result_text = event.get("result", "")
                if stream:
                    print(result_text, flush=True)

    proc.wait()
    completed_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with md_log_path.open("a", encoding="utf-8") as md_file:
        md_file.write(f"\nCompleted: {completed_ts}\n")
    stderr_text = proc.stderr.read()  # type: ignore[union-attr]
    if stderr_text:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps({"type": "stderr", "text": stderr_text}) + "\n")

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            proc.args,
            stderr=f"{stderr_text}\n(exit code {proc.returncode})",
        )

    return result_text


def _format_work_summaries(summaries: list[str]) -> str:
    """Format one or more work summaries for embedding in a developer-fix prompt."""
    if len(summaries) == 1:
        return summaries[0]
    parts = []
    for i, s in enumerate(summaries, start=1):
        label = "Implementation summary" if i == 1 else f"Fix summary {i - 1}"
        parts.append(f"### {label}\n\n{s.strip()}")
    return "\n\n---\n\n".join(parts)


def run_validate_script(script_name: str) -> tuple[bool, Path, str]:
    """Run a validate script from the scripts/ directory.

    Logs full output to a timestamped file under .claude/logs/dev-team/.
    Nothing is streamed to the console — callers print their own status line.
    Returns (success, log_path, tail) where tail is the last 30 lines.
    """
    scripts_dir = REPO_ROOT / "scripts"
    ext = ".cmd" if sys.platform == "win32" else ".sh"
    script_path = scripts_dir / f"{script_name}{ext}"

    if not script_path.exists():
        raise FileNotFoundError(
            f"Validate script not found: scripts/{script_name}{ext}"
        )

    timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    log_dir = REPO_ROOT / ".claude" / "logs" / "dev-team"
    log_dir.mkdir(parents=True, exist_ok=True)
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
    """Find the unique _spec_*.md file that contains the work item ID.

    Raises SystemExit(1) with a clear message on zero or multiple matches.
    """
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
        description="dev-team pipeline orchestrator",
    )
    parser.add_argument("work_item_id", metavar="work-item-id",
                        help="Work item ID (e.g. ADR-172 or Issue-444)")
    parser.add_argument("--workflow", required=True, metavar="path",
                        help="Path to a Mermaid stateDiagram-v2 workflow file")
    parser.add_argument("--research-skill", required=True, metavar="skill",
                        help="Researcher skill to use (e.g. researcher-plan or researcher-issue)")
    parser.add_argument("--plugin-root", metavar="path", default=None,
                        help="Plugin installation root (agents/ and commands/ resolved here)")
    args = parser.parse_args()

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

    log_dir = REPO_ROOT / ".claude" / "logs" / "dev-team"
    log_dir.mkdir(parents=True, exist_ok=True)
    context_path = log_dir / f"{work_item_id}-context.md"

    if context_path.exists():
        ctx = PipelineContext.load(context_path)
        if ctx.state in workflow.terminal_states:
            print(f"Previous run ended with state '{ctx.state}'.")
            print(f"Delete {context_path} to run again.")
            sys.exit(0 if ctx.state == "done" else 1)
        print(f"Resuming {work_item_id} from state '{ctx.state}'...", flush=True)
    else:
        ctx = PipelineContext(work_item_id=work_item_id, state=workflow.initial_state)
        ctx.save(context_path)

    if not ctx.base_branch:
        try:
            subprocess.run(
                ["git", "fetch", "--all"], check=True, cwd=REPO_ROOT, capture_output=True,
            )
            r = subprocess.run(
                ["git", "branch", "-r"],
                check=True, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            )
            remote_branches = [b.strip() for b in r.stdout.splitlines() if b.strip()]
            feature_branches = [
                b.replace("origin/", "", 1)
                for b in remote_branches
                if b.startswith("origin/feature/")
            ]
            candidates = ["main"] + [b for b in feature_branches if b != "main"]

            best_candidate = "main"
            best_count: int | None = None
            for candidate in candidates:
                try:
                    rc = subprocess.run(
                        ["git", "rev-list", "--count", f"origin/{candidate}..HEAD"],
                        check=True, cwd=REPO_ROOT, capture_output=True,
                        text=True, encoding="utf-8",
                    )
                    count = int(rc.stdout.strip())
                    if best_count is None or count < best_count:
                        best_count = count
                        best_candidate = candidate
                except (subprocess.CalledProcessError, ValueError):
                    continue

            ctx.base_branch = best_candidate
        except subprocess.CalledProcessError:
            ctx.base_branch = "main"
        ctx.save(context_path)

    DevTeamPipeline(ctx, context_path, workflow, research_skill=args.research_skill).run()


if __name__ == "__main__":
    main()
