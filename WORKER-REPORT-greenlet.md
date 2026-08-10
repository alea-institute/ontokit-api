# U7 suggester submit MissingGreenlet fix

## Root cause

Confirmed, with one correction to the initial path analysis: `_create_pr_directly` does not
commit on its successful path; it flushes and refreshes the new `PullRequest`. The underlying
bug is still that `_create_pr_for_session` treats both PR creation results alike even though the
editor path returns a detached Pydantic `PRResponse` and the authenticated-suggester fallback
returns a live ORM `PullRequest`. After the fallback, `_schedule_auto_accept` and notification
creation perform more async database work, and the final commit is followed by another read of
`pr_response`. If that ORM instance has been expired, those reads attempt an implicit async
refresh from ordinary attribute access and SQLAlchemy raises `MissingGreenlet`.

The existing-PR idempotency branch had the same smaller hazard: it reread the ORM PR after a
commit. It now snapshots its response fields before the commit as well.

## Diff summary

- Snapshot PR id, number, title, URL, and submitter user id immediately after either PR-creation
  branch returns, before scheduling, notifications, or commits.
- Use only those scalar snapshots for the session update, notification, and submit response.
- Snapshot existing-PR response fields before the idempotency branch commits.
- Add `test_suggester_submit_snapshots_direct_pr_before_later_db_work`, which gives the project
  member the `suggester` role, drives the 403 direct-PR fallback, expires the returned ORM-like
  object at the next database query, and asserts submission returns PR number 6 with status
  `submitted` without `MissingGreenlet`.

The anonymous submit caller and auto-submit caller still receive the unchanged
`SuggestionSubmitResponse` contract; editor PR creation behavior is unchanged.

## Verification

- Red-first targeted command attempted:
  `pytest tests/unit/test_suggestion_service.py::TestSubmit::test_suggester_submit_snapshots_direct_pr_before_later_db_work -q`
  - Test collection was blocked before execution: `ModuleNotFoundError: No module named 'pygit2'`.
- Retried through the project runner with a worktree-local cache:
  `UV_CACHE_DIR=.uv-cache uv run pytest tests/unit/test_suggestion_service.py::TestSubmit::test_suggester_submit_snapshots_direct_pr_before_later_db_work -q`
  - Dependency resolution was blocked because SQLAlchemy was not cached and network access is
    prohibited (`failed to download sqlalchemy==2.0.51`, DNS unavailable).
- `git diff --check`: passed.
- Python compilation of the changed source and test: passed.

## Deviations

The requested suggestion-service test module could not be executed in this worktree: the system
Python lacks project dependencies, and the worktree did not have a populated virtualenv. No
network access was requested or used, per the hard constraint. The repository's real-DB fixture
also sets `expire_on_commit=False`, so the regression uses the existing mocked suggestion-service
harness with an ORM-like expiry seam to deterministically exercise the production failure mode.
