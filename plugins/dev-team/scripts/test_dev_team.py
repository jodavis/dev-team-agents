"""Unit tests for ADR-253/ADR-254: ReviewComment/ReviewThread dataclasses, PipelineContext
serialisation changes, parse_json_list_output, and pipeline step logic."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dev_team import (
    FixPrStep,
    PipelineContext,
    ReviewComment,
    ReviewThread,
    SignoffStep,
    ValidateStep,
    parse_json_list_output,
)


class TestReviewCommentDataclass:
    def test_fields(self):
        rc = ReviewComment(author="Reviewer", comment="Fix this.")
        assert rc.author == "Reviewer"
        assert rc.comment == "Fix this."


class TestReviewThreadDataclass:
    def test_fields(self):
        rt = ReviewThread(
            id="abcd1234",
            file_path="src/foo.py",
            line_number=42,
            resolved=False,
        )
        assert rt.id == "abcd1234"
        assert rt.file_path == "src/foo.py"
        assert rt.line_number == 42
        assert rt.resolved is False
        assert rt.comments == []

    def test_comments_default_factory_not_shared(self):
        t1 = ReviewThread(id="a", file_path="f", line_number=1, resolved=False)
        t2 = ReviewThread(id="b", file_path="g", line_number=2, resolved=True)
        t1.comments.append(ReviewComment(author="Reviewer", comment="x"))
        assert len(t2.comments) == 0


class TestPipelineContextFirstPushDone:
    def test_default_is_false(self):
        ctx = PipelineContext(work_item_id="ADR-1")
        assert ctx.first_push_done is False

    def test_save_false_writes_lowercase(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", first_push_done=False)
        p = tmp_path / "ctx.md"
        ctx.save(p)
        content = p.read_text(encoding="utf-8")
        assert "first_push_done: false" in content
        assert "first_push_done: False" not in content

    def test_save_true_writes_lowercase(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", first_push_done=True)
        p = tmp_path / "ctx.md"
        ctx.save(p)
        content = p.read_text(encoding="utf-8")
        assert "first_push_done: true" in content
        assert "first_push_done: True" not in content

    def test_round_trip_true(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", first_push_done=True)
        p = tmp_path / "ctx.md"
        ctx.save(p)
        loaded = PipelineContext.load(p)
        assert loaded.first_push_done is True

    def test_round_trip_false(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", first_push_done=False)
        p = tmp_path / "ctx.md"
        ctx.save(p)
        loaded = PipelineContext.load(p)
        assert loaded.first_push_done is False

    def test_load_missing_key_defaults_false(self, tmp_path):
        p = tmp_path / "ctx.md"
        p.write_text("---\nwork_item_id: ADR-1\nstate: init\n---\n", encoding="utf-8")
        loaded = PipelineContext.load(p)
        assert loaded.first_push_done is False


class TestPipelineContextBaseBranch:
    def test_default_is_empty(self):
        ctx = PipelineContext(work_item_id="ADR-1")
        assert ctx.base_branch == ""

    def test_round_trip(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", base_branch="feature/ADR-246")
        p = tmp_path / "ctx.md"
        ctx.save(p)
        loaded = PipelineContext.load(p)
        assert loaded.base_branch == "feature/ADR-246"

    def test_load_missing_key_defaults_empty(self, tmp_path):
        p = tmp_path / "ctx.md"
        p.write_text("---\nwork_item_id: ADR-1\nstate: init\n---\n", encoding="utf-8")
        loaded = PipelineContext.load(p)
        assert loaded.base_branch == ""


class TestPipelineContextReviewThreads:
    def test_default_is_empty_list(self):
        ctx = PipelineContext(work_item_id="ADR-1")
        assert ctx.review_threads == []

    def test_save_writes_section_even_when_empty(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", review_threads=[])
        p = tmp_path / "ctx.md"
        ctx.save(p)
        content = p.read_text(encoding="utf-8")
        assert "<!-- section:Review Threads -->" in content

    def test_round_trip_empty_list(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", review_threads=[])
        p = tmp_path / "ctx.md"
        ctx.save(p)
        loaded = PipelineContext.load(p)
        assert loaded.review_threads == []

    def test_round_trip_with_threads(self, tmp_path):
        thread = ReviewThread(
            id="abcd1234",
            file_path="src/foo.py",
            line_number=10,
            resolved=False,
            comments=[ReviewComment(author="Reviewer", comment="Fix this.")],
        )
        ctx = PipelineContext(work_item_id="ADR-1", review_threads=[thread])
        p = tmp_path / "ctx.md"
        ctx.save(p)
        loaded = PipelineContext.load(p)
        assert len(loaded.review_threads) == 1
        t = loaded.review_threads[0]
        assert t.id == "abcd1234"
        assert t.file_path == "src/foo.py"
        assert t.line_number == 10
        assert t.resolved is False
        assert len(t.comments) == 1
        assert t.comments[0].author == "Reviewer"
        assert t.comments[0].comment == "Fix this."

    def test_serialized_json_uses_camelcase(self, tmp_path):
        thread = ReviewThread(
            id="xyz",
            file_path="lib/bar.py",
            line_number=5,
            resolved=True,
        )
        ctx = PipelineContext(work_item_id="ADR-1", review_threads=[thread])
        p = tmp_path / "ctx.md"
        ctx.save(p)
        content = p.read_text(encoding="utf-8")
        after_sentinel = content.split("<!-- section:Review Threads -->")[1]
        assert "filePath" in after_sentinel
        assert "lineNumber" in after_sentinel
        assert "file_path" not in after_sentinel
        assert "line_number" not in after_sentinel

    def test_load_non_list_json_in_section_returns_empty(self, tmp_path):
        # Valid JSON that is not a list (null, dict, string) must not crash load()
        for idx, bad_value in enumerate(["null", '{"key": "value"}', '"just a string"']):
            p = tmp_path / f"ctx_{idx}.md"
            p.write_text(
                "---\nwork_item_id: ADR-1\nstate: init\n---\n"
                f"<!-- section:Review Threads -->\n{bad_value}\n",
                encoding="utf-8",
            )
            loaded = PipelineContext.load(p)
            assert loaded.review_threads == [], f"Expected [] for bad_value={bad_value!r}"

    def test_no_pr_url_field(self):
        ctx = PipelineContext(work_item_id="ADR-1")
        assert not hasattr(ctx, "pr_url")

    def test_save_does_not_write_pr_url(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1")
        p = tmp_path / "ctx.md"
        ctx.save(p)
        content = p.read_text(encoding="utf-8")
        assert "pr_url" not in content

    def test_round_trip_multiple_threads(self, tmp_path):
        threads = [
            ReviewThread(id="aaa", file_path="a.py", line_number=1, resolved=False),
            ReviewThread(id="bbb", file_path="b.py", line_number=2, resolved=True,
                         comments=[ReviewComment(author="Developer", comment="Done.")]),
        ]
        ctx = PipelineContext(work_item_id="ADR-1", review_threads=threads)
        p = tmp_path / "ctx.md"
        ctx.save(p)
        loaded = PipelineContext.load(p)
        assert len(loaded.review_threads) == 2
        assert loaded.review_threads[0].id == "aaa"
        assert loaded.review_threads[1].id == "bbb"
        assert loaded.review_threads[1].comments[0].author == "Developer"


class TestPipelineContextReviewNotesUnconditional:
    def test_empty_review_notes_still_written(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", review_notes="")
        p = tmp_path / "ctx.md"
        ctx.save(p)
        content = p.read_text(encoding="utf-8")
        assert "<!-- section:Review Notes -->" in content


class TestParseJsonListOutput:
    def test_fenced_json_block_returns_list(self):
        text = '```json\n["a", "b"]\n```'
        assert parse_json_list_output(text) == ["a", "b"]

    def test_fenced_block_without_language_tag(self):
        text = '```\n[1, 2, 3]\n```'
        assert parse_json_list_output(text) == [1, 2, 3]

    def test_fenced_dict_skipped_falls_back_to_bare_line(self):
        text = '```json\n{"key": "val"}\n```\n["ok"]'
        assert parse_json_list_output(text) == ["ok"]

    def test_bare_json_array_line(self):
        text = 'some preamble\n["x", "y"]'
        assert parse_json_list_output(text) == ["x", "y"]

    def test_returns_empty_when_no_json(self):
        assert parse_json_list_output("no json here") == []

    def test_returns_empty_on_empty_string(self):
        assert parse_json_list_output("") == []

    def test_returns_empty_on_invalid_json(self):
        assert parse_json_list_output("[not valid json") == []

    def test_fenced_block_takes_priority_over_bare_line(self):
        # bare list comes first in text, fenced list follows — fenced wins
        text = '["bare"]\n```json\n["fenced"]\n```'
        assert parse_json_list_output(text) == ["fenced"]

    def test_uses_last_valid_fenced_block(self):
        text = '```json\n["first"]\n```\n```json\n["last"]\n```'
        assert parse_json_list_output(text) == ["last"]

    def test_uses_last_valid_bare_line(self):
        text = '["first"]\n["last"]'
        assert parse_json_list_output(text) == ["last"]

    def test_bare_dict_line_not_returned(self):
        assert parse_json_list_output('{"key": "val"}') == []

    def test_invalid_fenced_json_returns_empty(self):
        assert parse_json_list_output("```json\n{broken\n```") == []

    def test_fenced_block_with_objects_not_list_returns_empty(self):
        text = '```json\n{"items": []}\n```'
        assert parse_json_list_output(text) == []


# ---------------------------------------------------------------------------
# ADR-254 tests
# ---------------------------------------------------------------------------


class TestPipelineContextSignoffNotes:
    def test_default_is_empty(self):
        ctx = PipelineContext(work_item_id="ADR-1")
        assert ctx.signoff_notes == ""

    def test_save_writes_section_unconditionally(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", signoff_notes="")
        p = tmp_path / "ctx.md"
        ctx.save(p)
        content = p.read_text(encoding="utf-8")
        assert "<!-- section:Signoff Notes -->" in content

    def test_round_trip_empty(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", signoff_notes="")
        p = tmp_path / "ctx.md"
        ctx.save(p)
        loaded = PipelineContext.load(p)
        assert loaded.signoff_notes == ""

    def test_round_trip_with_content(self, tmp_path):
        ctx = PipelineContext(work_item_id="ADR-1", signoff_notes="LGTM overall.")
        p = tmp_path / "ctx.md"
        ctx.save(p)
        loaded = PipelineContext.load(p)
        assert loaded.signoff_notes == "LGTM overall."

    def test_load_missing_key_defaults_empty(self, tmp_path):
        p = tmp_path / "ctx.md"
        p.write_text("---\nwork_item_id: ADR-1\nstate: init\n---\n", encoding="utf-8")
        loaded = PipelineContext.load(p)
        assert loaded.signoff_notes == ""


class TestFixPrStepThreadMerge:
    """Thread merge logic in FixPrStep.run(): comments replaced, resolved preserved,
    unknown IDs silently discarded."""

    def _make_ctx(self, threads, tmp_path):
        ctx = PipelineContext(
            work_item_id="ADR-254",
            brief="Do the thing",
            work_summaries=["Summary 1"],
            review_threads=threads,
        )
        p = tmp_path / "ctx.md"
        ctx.save(p)
        return ctx, p

    def _run(self, ctx, context_path, returned_threads):
        fix_summary = f"```json\n{json.dumps(returned_threads)}\n```"
        with patch("dev_team.call_agent", return_value=fix_summary):
            return FixPrStep(context_path).run(ctx)

    def test_comments_replaced_on_match(self, tmp_path):
        existing = ReviewThread(
            id="abc12345",
            file_path="src/foo.py",
            line_number=10,
            resolved=False,
            comments=[ReviewComment(author="Reviewer", comment="Fix this.")],
        )
        ctx, path = self._make_ctx([existing], tmp_path)

        returned = [{
            "id": "abc12345",
            "filePath": "src/foo.py",
            "lineNumber": 10,
            "resolved": True,
            "comments": [
                {"author": "Reviewer", "comment": "Fix this."},
                {"author": "Developer", "comment": "Fixed in commit abc."},
            ],
        }]
        trigger = self._run(ctx, path, returned)

        assert trigger == "fix_done"
        thread = ctx.review_threads[0]
        assert len(thread.comments) == 2
        assert thread.comments[0].author == "Reviewer"
        assert thread.comments[1].author == "Developer"
        assert thread.comments[1].comment == "Fixed in commit abc."

    def test_resolved_preserved_from_existing_thread(self, tmp_path):
        existing = ReviewThread(
            id="def67890",
            file_path="src/bar.py",
            line_number=5,
            resolved=False,
            comments=[ReviewComment(author="Reviewer", comment="Issue here.")],
        )
        ctx, path = self._make_ctx([existing], tmp_path)

        returned = [{
            "id": "def67890",
            "resolved": True,  # Developer claims resolved — must be ignored
            "comments": [
                {"author": "Reviewer", "comment": "Issue here."},
                {"author": "Developer", "comment": "Fixed."},
            ],
        }]
        self._run(ctx, path, returned)

        assert ctx.review_threads[0].resolved is False

    def test_unknown_ids_silently_discarded(self, tmp_path):
        existing = ReviewThread(
            id="known111",
            file_path="src/baz.py",
            line_number=1,
            resolved=False,
            comments=[ReviewComment(author="Reviewer", comment="Check this.")],
        )
        ctx, path = self._make_ctx([existing], tmp_path)

        returned = [{
            "id": "unknown9",
            "filePath": "src/new.py",
            "lineNumber": 2,
            "resolved": True,
            "comments": [{"author": "Developer", "comment": "New."}],
        }]
        self._run(ctx, path, returned)

        assert len(ctx.review_threads) == 1
        assert ctx.review_threads[0].id == "known111"
        assert len(ctx.review_threads[0].comments) == 1


class TestSignoffStepThreadMerge:
    """Thread merge logic in SignoffStep.run(): comments appended, resolved updated,
    empty comments only update resolved, new threads added with UUIDs."""

    def _make_ctx(self, threads, tmp_path):
        ctx = PipelineContext(
            work_item_id="ADR-254",
            brief="Brief",
            work_summaries=["Summary 1"],
            review_threads=threads,
            base_branch="main",
        )
        p = tmp_path / "ctx.md"
        ctx.save(p)
        return ctx, p

    def _reviewer_json(self, threads, body="body", status="approved"):
        return json.dumps({"status": status, "threads": threads, "body": body})

    def _run_signoff(self, ctx, context_path, reviewer_output):
        def _call_agent(agent, skill, *args, **kwargs):
            if skill == "reviewer-sign-off":
                return reviewer_output
            return "inconclusive"

        with patch("dev_team._commit_and_push"), \
             patch("dev_team.run_validate_script", return_value=(True, Path("log.txt"), "")), \
             patch("dev_team.call_agent", side_effect=_call_agent):
            return SignoffStep(context_path).run(ctx)

    def test_comments_appended_not_replaced(self, tmp_path):
        existing = ReviewThread(
            id="thr00001",
            file_path="src/a.py",
            line_number=1,
            resolved=False,
            comments=[ReviewComment(author="Reviewer", comment="Original.")],
        )
        ctx, path = self._make_ctx([existing], tmp_path)

        sign_off = [{"id": "thr00001", "resolved": True,
                     "comments": [{"author": "Reviewer", "comment": "Looks good."}]}]
        self._run_signoff(ctx, path, self._reviewer_json(sign_off))

        thread = ctx.review_threads[0]
        assert len(thread.comments) == 2
        assert thread.comments[0].comment == "Original."
        assert thread.comments[1].comment == "Looks good."

    def test_resolved_is_updated(self, tmp_path):
        existing = ReviewThread(
            id="thr00002",
            file_path="src/b.py",
            line_number=2,
            resolved=False,
            comments=[ReviewComment(author="Reviewer", comment="Fix x.")],
        )
        ctx, path = self._make_ctx([existing], tmp_path)

        sign_off = [{"id": "thr00002", "resolved": True,
                     "comments": [{"author": "Reviewer", "comment": "All good."}]}]
        self._run_signoff(ctx, path, self._reviewer_json(sign_off))

        assert ctx.review_threads[0].resolved is True

    def test_empty_comments_only_updates_resolved(self, tmp_path):
        existing = ReviewThread(
            id="thr00003",
            file_path="src/c.py",
            line_number=3,
            resolved=False,
            comments=[ReviewComment(author="Reviewer", comment="Something.")],
        )
        ctx, path = self._make_ctx([existing], tmp_path)

        sign_off = [{"id": "thr00003", "resolved": True, "comments": []}]
        self._run_signoff(ctx, path, self._reviewer_json(sign_off))

        thread = ctx.review_threads[0]
        assert thread.resolved is True
        assert len(thread.comments) == 1  # original unchanged

    def test_new_threads_added_with_uuids(self, tmp_path):
        ctx, path = self._make_ctx([], tmp_path)

        sign_off = [{"filePath": "src/new.py", "lineNumber": 10, "resolved": False,
                     "comments": [{"author": "Reviewer", "comment": "New issue."}]}]
        self._run_signoff(ctx, path, self._reviewer_json(sign_off, status="changes_requested"))

        assert len(ctx.review_threads) == 1
        new_thread = ctx.review_threads[0]
        assert len(new_thread.id) == 8
        assert new_thread.file_path == "src/new.py"
        assert new_thread.resolved is False


class TestSignoffStepExitGuard:
    """sys.exit(1) is called in the main thread when reviewer fails or returns no threads."""

    def _make_ctx(self, tmp_path):
        ctx = PipelineContext(
            work_item_id="ADR-254",
            brief="Brief",
            work_summaries=["Summary"],
            base_branch="main",
            review_threads=[
                ReviewThread(id="t1", file_path="f.py", line_number=1, resolved=True)
            ],
        )
        p = tmp_path / "ctx.md"
        ctx.save(p)
        return ctx, p

    def test_exits_when_reviewer_agent_raises(self, tmp_path):
        ctx, path = self._make_ctx(tmp_path)

        def _call_agent(agent, skill, *args, **kwargs):
            if skill == "reviewer-sign-off":
                raise RuntimeError("Agent failed")
            return "inconclusive"

        with patch("dev_team._commit_and_push"), \
             patch("dev_team.run_validate_script", return_value=(True, Path("log.txt"), "")), \
             patch("dev_team.call_agent", side_effect=_call_agent):
            with pytest.raises(SystemExit) as exc_info:
                SignoffStep(path).run(ctx)
        assert exc_info.value.code == 1

    def test_exits_when_reviewer_returns_empty_threads(self, tmp_path):
        ctx, path = self._make_ctx(tmp_path)
        empty_output = '{"status": "approved", "threads": [], "body": ""}'

        def _call_agent(agent, skill, *args, **kwargs):
            if skill == "reviewer-sign-off":
                return empty_output
            return "inconclusive"

        with patch("dev_team._commit_and_push"), \
             patch("dev_team.run_validate_script", return_value=(True, Path("log.txt"), "")), \
             patch("dev_team.call_agent", side_effect=_call_agent):
            with pytest.raises(SystemExit) as exc_info:
                SignoffStep(path).run(ctx)
        assert exc_info.value.code == 1


class TestValidateStepFirstPush:
    """first_push_done transitions False→True and the marker is emitted exactly once."""

    def _make_ctx(self, first_push_done, tmp_path):
        ctx = PipelineContext(
            work_item_id="ADR-254",
            first_push_done=first_push_done,
        )
        p = tmp_path / "ctx.md"
        ctx.save(p)
        return ctx, p

    def test_sets_first_push_done_to_true(self, tmp_path):
        ctx, path = self._make_ctx(first_push_done=False, tmp_path=tmp_path)

        with patch("dev_team.run_validate_script", return_value=(True, Path("log.txt"), "")), \
             patch("dev_team._commit_and_push"):
            trigger = ValidateStep(path).run(ctx)

        assert trigger == "clean"
        assert ctx.first_push_done is True

    def test_marker_emitted_when_first_push_done_false(self, tmp_path, capsys):
        ctx, path = self._make_ctx(first_push_done=False, tmp_path=tmp_path)

        with patch("dev_team.run_validate_script", return_value=(True, Path("log.txt"), "")), \
             patch("dev_team._commit_and_push"):
            ValidateStep(path).run(ctx)

        assert "[DEV-TEAM] First push complete" in capsys.readouterr().out

    def test_marker_not_emitted_when_already_done(self, tmp_path, capsys):
        ctx, path = self._make_ctx(first_push_done=True, tmp_path=tmp_path)

        with patch("dev_team.run_validate_script", return_value=(True, Path("log.txt"), "")), \
             patch("dev_team._commit_and_push"):
            ValidateStep(path).run(ctx)

        assert "[DEV-TEAM] First push complete" not in capsys.readouterr().out
