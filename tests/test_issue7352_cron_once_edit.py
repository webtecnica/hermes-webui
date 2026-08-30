"""Regression coverage for issue #7352: editing one-shot cron jobs.

One-shot jobs store a canonical ISO ``run_at`` timestamp in
``job.schedule.run_at``, but the UI pre-filled the edit form with the
human-readable ``schedule_display`` (e.g. ``once at 2026-08-28 16:00``),
which the agent's schedule parser rejects. Saving then raised an uncaught
``ValueError`` in the cron-update route and returned HTTP 500.
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = PANELS_JS.find(marker)
    assert start != -1, f"{name} not found"
    paren = PANELS_JS.find("(", start)
    assert paren != -1, f"{name} params not found"
    depth = 0
    for idx in range(paren, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = PANELS_JS.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[brace + 1 : idx]
    raise AssertionError(f"{name} body did not terminate")


# --- Frontend: schedule value used to pre-fill the edit form ---------------


def test_cron_schedule_for_edit_returns_run_at_for_once_jobs():
    helper = _function_body("_cronScheduleForEdit")
    # Case 1: once-kind jobs pre-fill from the canonical ISO run_at timestamp.
    assert "job.schedule.kind === 'once'" in helper
    assert "job.schedule.run_at" in helper
    # Case 6: non-once jobs keep schedule_display as the primary fallback.
    assert "job.schedule_display" in helper
    assert "job.schedule.expr" in helper
    assert "job.schedule.expression" in helper


def test_cron_open_edit_uses_round_trip_schedule_not_display():
    # Case 1: openCronEdit feeds the parseable schedule into the form.
    body = _function_body("openCronEdit")
    assert "schedule: _cronScheduleForEdit(job)" in body
    assert "schedule_display ||" not in body


def test_cron_duplicate_uses_round_trip_schedule_not_display():
    # Case 4: duplicating a one-shot job pre-fills a parseable schedule value.
    body = _function_body("duplicateCurrentCron")
    assert "schedule: _cronScheduleForEdit(job)" in body
    assert "schedule_display ||" not in body


# --- Backend: cron-update route validation ----------------------------------


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _install_fake_cron_jobs(monkeypatch, update_job_impl):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.update_job = update_job_impl
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)


def test_cron_update_invalid_schedule_returns_400_not_500(monkeypatch):
    # Case 5: schedule validation failures surface as HTTP 400 with the
    # parser message instead of an uncaught ValueError → HTTP 500.
    import api.routes as routes

    def _update_job(job_id, updates):
        raise ValueError(
            "Invalid schedule 'once at 2026-08-28 16:05'. "
            "Use: - Timestamp: '2026-02-03T14:00:00' (one-shot at time)"
        )

    _install_fake_cron_jobs(monkeypatch, _update_job)
    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {"job_id": "job7352", "schedule": "once at 2026-08-28 16:05"},
    )
    assert handler.status == 400
    assert "Invalid schedule" in _payload(handler)["error"]


def test_cron_update_valid_iso_timestamp_succeeds(monkeypatch):
    # Cases 2 & 3: an unchanged or time-only-edited one-shot schedule
    # (canonical ISO timestamp) updates the job and returns 200.
    import api.routes as routes

    calls = []

    def _update_job(job_id, updates):
        calls.append((job_id, updates))
        return {
            "id": job_id,
            "name": "One-shot",
            "schedule": {"kind": "once", "run_at": "2026-08-28T16:05:00-05:00"},
        }

    _install_fake_cron_jobs(monkeypatch, _update_job)
    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {"job_id": "job7352", "schedule": "2026-08-28T16:05:00-05:00"},
    )
    assert handler.status == 200
    assert calls == [("job7352", {"schedule": "2026-08-28T16:05:00-05:00"})]
    assert _payload(handler)["job"]["schedule"]["run_at"] == "2026-08-28T16:05:00-05:00"
