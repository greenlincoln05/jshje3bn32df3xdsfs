"""Offline tests for the PID-file lock that stops two `btcbot paper`/`btcbot demo` instances from running
against the same data directory at once (see src/btcbot/singleton.py's docstring for the production
incident -- two demo processes independently entering the same window -- this closes off)."""

import json
import os

import pytest

from btcbot.singleton import AlreadyRunningError, _pid_is_alive, acquire_lock, release_lock

DEAD_PID = 999_999_999  # far outside any real process id range on this machine


class TestPidIsAlive:
    def test_the_current_process_is_alive(self):
        assert _pid_is_alive(os.getpid()) is True

    def test_a_nonexistent_pid_is_not_alive(self):
        assert _pid_is_alive(DEAD_PID) is False


class TestAcquireLock:
    def test_a_fresh_lock_path_is_claimed(self, tmp_path):
        lock_path = tmp_path / ".demo.lock"
        acquire_lock(lock_path, label="btcbot demo")
        held = json.loads(lock_path.read_text())
        assert held["pid"] == os.getpid()
        assert "started_at" in held

    def test_creates_missing_parent_directories(self, tmp_path):
        lock_path = tmp_path / "nested" / "dir" / ".demo.lock"
        acquire_lock(lock_path, label="btcbot demo")
        assert lock_path.exists()

    def test_refuses_when_a_live_process_already_holds_it(self, tmp_path):
        lock_path = tmp_path / ".demo.lock"
        lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-09-23T18:18:17+00:00"}))
        with pytest.raises(AlreadyRunningError, match=r"btcbot demo is already running \(PID \d+"):
            acquire_lock(lock_path, label="btcbot demo")
        # refusing must not overwrite the existing (still-valid) lock
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_a_lock_naming_a_dead_pid_is_silently_reclaimed(self, tmp_path):
        # e.g. the process holding it was killed rather than shut down cleanly -- a stale lock must never
        # permanently block every future restart.
        lock_path = tmp_path / ".demo.lock"
        lock_path.write_text(json.dumps({"pid": DEAD_PID, "started_at": "2026-09-20T00:00:00+00:00"}))
        acquire_lock(lock_path, label="btcbot demo")  # does not raise
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_a_corrupt_lock_file_is_treated_as_stale_not_fatal(self, tmp_path):
        lock_path = tmp_path / ".demo.lock"
        lock_path.write_text("not valid json at all")
        acquire_lock(lock_path, label="btcbot demo")
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_a_lock_missing_the_pid_field_is_treated_as_stale(self, tmp_path):
        lock_path = tmp_path / ".demo.lock"
        lock_path.write_text(json.dumps({"started_at": "2026-09-20T00:00:00+00:00"}))
        acquire_lock(lock_path, label="btcbot demo")
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_the_error_message_names_the_lock_path_and_how_to_recover(self, tmp_path):
        lock_path = tmp_path / ".demo.lock"
        lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-09-23T18:18:17+00:00"}))
        with pytest.raises(AlreadyRunningError) as excinfo:
            acquire_lock(lock_path, label="btcbot demo")
        assert str(lock_path) in str(excinfo.value)
        assert "2026-09-23T18:18:17+00:00" in str(excinfo.value)


class TestReleaseLock:
    def test_removes_a_lock_this_process_holds(self, tmp_path):
        lock_path = tmp_path / ".demo.lock"
        acquire_lock(lock_path, label="btcbot demo")
        release_lock(lock_path)
        assert not lock_path.exists()

    def test_a_missing_lock_path_is_a_no_op(self, tmp_path):
        release_lock(tmp_path / "never-existed.lock")  # must not raise

    def test_never_removes_a_lock_held_by_a_different_pid(self, tmp_path):
        # e.g. a newer process already reclaimed a stale lock between this process acquiring it and
        # releasing it -- releasing must never delete state it doesn't actually own.
        lock_path = tmp_path / ".demo.lock"
        lock_path.write_text(json.dumps({"pid": os.getpid() + 1, "started_at": "2026-09-23T18:18:17+00:00"}))
        release_lock(lock_path)
        assert lock_path.exists()

    def test_a_corrupt_lock_file_is_left_alone_rather_than_raising(self, tmp_path):
        lock_path = tmp_path / ".demo.lock"
        lock_path.write_text("not valid json")
        release_lock(lock_path)  # must not raise
        assert lock_path.exists()  # unknown ownership: not deleted


class TestAcquireThenReleaseRoundTrip:
    def test_after_release_a_second_acquire_succeeds(self, tmp_path):
        lock_path = tmp_path / ".demo.lock"
        acquire_lock(lock_path, label="btcbot demo")
        release_lock(lock_path)
        acquire_lock(lock_path, label="btcbot demo")  # does not raise
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_paper_and_demo_use_independent_lock_files(self, tmp_path):
        # Two DIFFERENT commands (paper vs demo) against the same data dir must not contend with each
        # other -- only two instances of the SAME command should.
        paper_lock, demo_lock = tmp_path / ".paper.lock", tmp_path / ".demo.lock"
        acquire_lock(paper_lock, label="btcbot paper")
        acquire_lock(demo_lock, label="btcbot demo")  # does not raise: independent lock file
