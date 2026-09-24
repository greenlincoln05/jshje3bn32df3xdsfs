"""Offline tests for code_version.py: capturing which git commit/PR was live at a paper/demo run's start,
so the dashboard's Portfolio tab can mark where performance might have shifted because the code changed."""

import sqlite3
from datetime import datetime, timezone

from btcbot.code_version import (
    CodeVersion,
    _parse_git_log_fields,
    code_version_at,
    current_code_version,
    init_code_version_schema,
    load_code_versions,
    record_code_version,
)

FS = "\x1f"


def make_record(commit_hash="abc123", subject="Merge pull request #42 from owner/branch",
                 body="The actual PR title\n\nSome longer description.", ts="2026-09-23T10:00:00-04:00"):
    return f"{commit_hash}{FS}{subject}{FS}{body}{FS}{ts}"


class TestParseGitLogFields:
    def test_parses_a_well_formed_merge_commit_record(self):
        v = _parse_git_log_fields(make_record())
        assert v.commit_hash == "abc123"
        assert v.pr_number == 42
        assert v.pr_title == "The actual PR title"
        assert v.commit_ts == datetime.fromisoformat("2026-09-23T10:00:00-04:00")

    def test_a_non_merge_subject_has_no_pr_number(self):
        v = _parse_git_log_fields(make_record(subject="Fix a typo in the README", body=""))
        assert v.pr_number is None
        assert v.pr_title is None

    def test_the_first_non_blank_body_line_is_the_title_even_with_leading_blank_lines(self):
        v = _parse_git_log_fields(make_record(body="\n\n  Real title here  \n\nmore text"))
        assert v.pr_title == "Real title here"

    def test_an_empty_body_gives_no_title(self):
        v = _parse_git_log_fields(make_record(body=""))
        assert v.pr_title is None

    def test_wrong_field_count_is_none_not_a_crash(self):
        assert _parse_git_log_fields("only-one-field") is None
        assert _parse_git_log_fields(f"a{FS}b{FS}c{FS}d{FS}e") is None

    def test_an_unparseable_timestamp_is_none(self):
        assert _parse_git_log_fields(make_record(ts="not-a-date")) is None


class TestCodeVersionLabel:
    def test_a_pr_commit_labels_with_pr_number_and_title(self):
        v = CodeVersion(commit_hash="abcdef1234567890", commit_subject="Merge pull request #7 from x/y",
                         pr_title="Add the thing", commit_ts=datetime.now(timezone.utc), pr_number=7)
        assert v.label == "PR #7: Add the thing"

    def test_a_pr_commit_with_no_captured_title_falls_back_to_the_subject(self):
        v = CodeVersion(commit_hash="abcdef1234567890", commit_subject="Merge pull request #7 from x/y",
                         pr_title=None, commit_ts=datetime.now(timezone.utc), pr_number=7)
        assert v.label == "PR #7: Merge pull request #7 from x/y"

    def test_a_non_pr_commit_labels_with_a_short_hash_and_subject(self):
        v = CodeVersion(commit_hash="abcdef1234567890", commit_subject="Fix a typo",
                         pr_title=None, commit_ts=datetime.now(timezone.utc), pr_number=None)
        assert v.label == "abcdef12: Fix a typo"


class TestGitIntegration:
    """Runs real `git log` against this repo -- structural checks only (no exact-value assertions), since
    the repo's own history changes over time."""

    def test_current_code_version_reads_a_real_commit_from_this_repo(self):
        v = current_code_version()
        assert v is not None
        assert len(v.commit_hash) == 40 and all(c in "0123456789abcdef" for c in v.commit_hash)
        assert v.commit_ts.tzinfo is not None

    def test_code_version_at_head_matches_current_code_version(self):
        assert code_version_at("HEAD").commit_hash == current_code_version().commit_hash

    def test_a_directory_with_no_git_repo_returns_none(self, tmp_path):
        assert code_version_at("HEAD", cwd=tmp_path) is None


class TestSchemaAndRoundTrip:
    def test_load_on_a_database_with_no_code_version_table_is_an_empty_list_not_an_error(self):
        conn = sqlite3.connect(":memory:")
        assert load_code_versions(conn) == []

    def test_record_then_load_round_trips_every_field(self):
        conn = sqlite3.connect(":memory:")
        init_code_version_schema(conn)
        v = CodeVersion(commit_hash="deadbeef" * 5, commit_subject="Merge pull request #9 from a/b",
                         pr_title="Some feature", commit_ts=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
                         pr_number=9)
        recorded_ts = datetime(2026, 9, 20, 12, 5, tzinfo=timezone.utc)
        record_code_version(conn, v, source="auto", recorded_ts=recorded_ts)

        loaded = load_code_versions(conn)
        assert len(loaded) == 1
        got_recorded_ts, got_version, got_source = loaded[0]
        assert got_recorded_ts == recorded_ts
        assert got_version == v
        assert got_source == "auto"

    def test_a_version_with_no_pr_number_round_trips_as_none(self):
        conn = sqlite3.connect(":memory:")
        init_code_version_schema(conn)
        v = CodeVersion(commit_hash="feedface" * 5, commit_subject="Fix a typo", pr_title=None,
                         commit_ts=datetime(2026, 9, 20, tzinfo=timezone.utc), pr_number=None)
        record_code_version(conn, v, source="manual")
        _, got_version, _ = load_code_versions(conn)[0]
        assert got_version.pr_number is None and got_version.pr_title is None

    def test_multiple_versions_load_oldest_recorded_first(self):
        conn = sqlite3.connect(":memory:")
        init_code_version_schema(conn)
        v1 = CodeVersion("aaaa" * 10, "s1", None, datetime(2026, 9, 19, tzinfo=timezone.utc), None)
        v2 = CodeVersion("bbbb" * 10, "s2", None, datetime(2026, 9, 20, tzinfo=timezone.utc), None)
        record_code_version(conn, v2, source="manual", recorded_ts=datetime(2026, 9, 20, tzinfo=timezone.utc))
        record_code_version(conn, v1, source="manual", recorded_ts=datetime(2026, 9, 19, tzinfo=timezone.utc))
        loaded = load_code_versions(conn)
        assert [v.commit_hash for _, v, _ in loaded] == [v1.commit_hash, v2.commit_hash]

    def test_auto_and_manual_sources_are_both_preserved(self):
        conn = sqlite3.connect(":memory:")
        init_code_version_schema(conn)
        v = CodeVersion("cccc" * 10, "s", None, datetime(2026, 9, 19, tzinfo=timezone.utc), None)
        record_code_version(conn, v, source="auto")
        sources = [source for _, _, source in load_code_versions(conn)]
        assert sources == ["auto"]

    def test_init_schema_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        init_code_version_schema(conn)
        init_code_version_schema(conn)  # must not raise on a second call
