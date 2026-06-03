"""Unit tests for ADR-253: ReviewComment/ReviewThread dataclasses, PipelineContext
serialisation changes, and parse_json_list_output."""

import json
from pathlib import Path

import pytest

from dev_team import (
    PipelineContext,
    ReviewComment,
    ReviewThread,
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
