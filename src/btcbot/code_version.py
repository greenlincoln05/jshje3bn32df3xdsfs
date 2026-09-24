"""Which code was running when: one ``code_version`` row per ``btcbot paper``/``btcbot demo`` run, recorded
at startup, so the dashboard's Portfolio tab can show one continuous trade history across every run's own
database file and mark where performance might have shifted because the code changed underneath it.

:func:`current_code_version` reads ``git`` directly (no network, no GitHub API): this repo's own merge
commits already carry both the PR number (subject: ``Merge pull request #N from owner/branch``) and the
PR's actual title (body: the first line, from how ``gh pr create --title`` shapes the merge), so both come
for free from a plain ``git log`` read. It must never raise or block a live trading run starting -- a git
failure (not a repo, git not on PATH, a detached worktree) yields a ``None`` version, and the caller simply
records nothing rather than aborting the run over metadata.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_PR_PATTERN = re.compile(r"Merge pull request #(\d+)")
_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"%H{_FIELD_SEP}%s{_FIELD_SEP}%b{_FIELD_SEP}%cI"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS code_version (
    id INTEGER PRIMARY KEY,
    recorded_ts TEXT NOT NULL,     -- when this row was written: the run's own start (or a backfill's guess)
    commit_hash TEXT NOT NULL,
    commit_subject TEXT NOT NULL,
    pr_title TEXT,                 -- the PR's own title (merge commit body), when there is one
    commit_ts TEXT NOT NULL,       -- when that commit was made, per git
    pr_number INTEGER,             -- parsed from the subject when it's a "Merge pull request #N" commit
    source TEXT NOT NULL           -- 'auto' (captured live at startup) | 'manual' (backfilled after the fact)
);
"""


@dataclass(frozen=True, slots=True)
class CodeVersion:
    commit_hash: str
    commit_subject: str
    pr_title: str | None
    commit_ts: datetime
    pr_number: int | None

    @property
    def label(self) -> str:
        """A short, human line for the dashboard: prefers the PR number and title (what the owner actually
        thinks in terms of) over a bare, meaningless-to-read commit hash."""
        if self.pr_number is not None:
            return f"PR #{self.pr_number}: {self.pr_title or self.commit_subject}"
        return f"{self.commit_hash[:8]}: {self.commit_subject}"


def current_code_version(*, cwd: Path | None = None) -> CodeVersion | None:
    """The commit at ``HEAD`` right now, or ``None`` if it can't be determined (git missing, not a repo,
    anything else) -- never raises, since this is metadata a live trading run must not depend on to start."""
    return code_version_at("HEAD", cwd=cwd)


def code_version_at(ref: str, *, cwd: Path | None = None) -> CodeVersion | None:
    """The commit ``ref`` resolves to (a hash, branch, tag, or ``HEAD``), or ``None`` if it can't be read.
    Used both for the live ``HEAD`` capture and, one commit at a time, by the one-off history backfill."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", ref, f"--format={_LOG_FORMAT}"],
            cwd=cwd, capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_git_log_fields(result.stdout)


def _parse_git_log_fields(text: str) -> CodeVersion | None:
    parts = text.split(_FIELD_SEP)
    if len(parts) != 4:
        return None
    commit_hash, subject, body, iso_ts = parts
    try:
        commit_ts = datetime.fromisoformat(iso_ts.strip())
    except ValueError:
        return None
    match = _PR_PATTERN.search(subject)
    pr_title = next((line.strip() for line in body.splitlines() if line.strip()), None)
    return CodeVersion(
        commit_hash=commit_hash.strip(), commit_subject=subject.strip(), pr_title=pr_title,
        commit_ts=commit_ts, pr_number=int(match.group(1)) if match else None,
    )


def init_code_version_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def record_code_version(
    conn: sqlite3.Connection, version: CodeVersion, *, source: str, recorded_ts: datetime | None = None
) -> None:
    recorded_ts = recorded_ts or datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO code_version (recorded_ts, commit_hash, commit_subject, pr_title, commit_ts, pr_number, source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            recorded_ts.astimezone(timezone.utc).isoformat(), version.commit_hash, version.commit_subject,
            version.pr_title, version.commit_ts.astimezone(timezone.utc).isoformat(), version.pr_number, source,
        ),
    )
    conn.commit()


def load_code_versions(conn: sqlite3.Connection) -> list[tuple[datetime, CodeVersion, str]]:
    """Every recorded version for this database, oldest first, as ``(recorded_ts, version, source)``. A
    database from before this feature existed (or where git wasn't available at startup) has none -- an
    empty list, not an error; the caller shows those runs' trades unmarked rather than failing."""
    try:
        rows = conn.execute(
            "SELECT recorded_ts, commit_hash, commit_subject, pr_title, commit_ts, pr_number, source "
            "FROM code_version ORDER BY recorded_ts"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        (
            datetime.fromisoformat(recorded_ts),
            CodeVersion(commit_hash=commit_hash, commit_subject=subject, pr_title=pr_title,
                        commit_ts=datetime.fromisoformat(commit_ts), pr_number=pr_number),
            source,
        )
        for recorded_ts, commit_hash, subject, pr_title, commit_ts, pr_number, source in rows
    ]
