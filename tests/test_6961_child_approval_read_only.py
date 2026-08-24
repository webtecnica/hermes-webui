"""Regression tests for #6961 round-3 re-gate (read-only end-to-end child approvals).

The r3 review found three MUST-FIXes:
1. (CORE) The surfaced child card was actionable and routed into the PARENT's
   legacy FIFO resolver — production child entries have no approval_id, so a
   click signalled the parent's approval while the child stayed pending.
   Fix: child projections carry a non-empty read-only sentinel approval_id and
   the legacy resolver rejects it.
2. (SILENT) Cross-profile leak: _pending/_gateway_queues are process-global,
   so an identical child id across two profiles could surface profile A's
   pending command under profile B's parent. Fix: entries are bound to the
   enqueuing profile's state-db and the projection filters by that identity.
3. (SILENT) Malformed model_config fell through to the wrong physical parent.
   Fix: only the authoritative (parsed/absent) config may take the
   physical-parent fallback; malformed JSON fails closed.
"""
import json
import pathlib
import sqlite3
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from api import route_approvals as ra  # noqa: E402
from api import routes as r  # noqa: E402

_SENTINEL = "__read_only_child__:"


def _clear(*session_keys: str) -> None:
    with ra._lock:
        for key in session_keys:
            r._pending.pop(key, None)
            r._gateway_queues.pop(key, None)
        ra._child_approval_parents.clear()


def _seed(parent: str, child: str) -> str:
    child_key = f"subagent:{child}"
    _clear(parent, child_key)
    ra.seed_child_parent(child, parent)
    return child_key


# ---------------------------------------------------------------------------
# MUST-FIX 1 (CORE) — read-only sentinel projection
# ---------------------------------------------------------------------------

def test_child_projection_carries_read_only_sentinel():
    """The projected child head must be inert: sentinel id + read_only flag."""
    parent = "test-6961-sentinel-parent"
    child = "test-6961-sentinel-child"
    child_key = _seed(parent, child)
    try:
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        with ra._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert head is not None
        assert head["command"] == "childcmd"
        assert head["read_only"] is True, "child projection must be flagged read-only"
        assert str(head.get("approval_id") or "").startswith(_SENTINEL), (
            "child projection must carry the non-empty read-only sentinel id, "
            "never the raw null/absent production approval_id"
        )
        assert total == 1
        # The underlying child entry keeps its own identity under the child key.
        with ra._lock:
            raw = r._pending[child_key][0]
        assert not str(raw.get("approval_id") or "").startswith(_SENTINEL)
    finally:
        _clear(parent, child_key)


def test_legacy_resolver_rejects_read_only_sentinel():
    """A sentinel approval_id must never resolve through the parent resolver."""
    parent = "test-6961-resolver-parent"
    child = "test-6961-resolver-child"
    child_key = _seed(parent, child)
    try:
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        with ra._lock:
            head, _total = ra.pending_head_for_session_locked(parent)
        assert head is not None
        sentinel_id = str(head["approval_id"])
        # Attempting to answer the surfaced child through the parent's legacy
        # FIFO resolver must fail closed (return False, nothing resolved).
        resolved = r._resolve_approval_legacy(parent, sentinel_id, "once")
        assert resolved is False
        # The child entry stays pending untouched.
        with ra._lock:
            assert len(r._pending[child_key]) == 1
    finally:
        _clear(parent, child_key)


def test_frontend_disables_controls_for_read_only_card():
    """messages.js must disable every approval control for sentinel cards."""
    src = pathlib.Path(REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert "_READ_ONLY_APPROVAL_PREFIX" in src
    assert "_setApprovalControlsDisabled(" in src
    # The read-only branch must disable controls unconditionally.
    assert "readOnly || responding" in src, (
        "read-only cards must disable every approval control"
    )
    # respondApproval must refuse sentinel ids outright.
    assert "_READ_ONLY_APPROVAL_PREFIX) === 0" in src


# ---------------------------------------------------------------------------
# MUST-FIX 2 (SILENT) — cross-profile provenance filter
# ---------------------------------------------------------------------------

def test_cross_profile_child_entry_not_projected(monkeypatch):
    """A child entry parked by profile A must never surface under profile B."""
    parent = "test-6961-prov-parent"
    child = "test-6961-prov-child"
    child_key = _seed(parent, child)
    try:
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "profileA-db")
        r.submit_pending(
            child_key,
            {"command": "acmd", "pattern_key": "ap", "pattern_keys": ["ap"], "description": "ad"},
        )
        # Same process, profile B active: the entry must not be projected.
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "profileB-db")
        with ra._lock:
            head_b, total_b = ra.pending_head_for_session_locked(parent)
        assert head_b is None and total_b == 0, (
            "cross-profile child entry must be filtered from the projection"
        )
        # Back on profile A the entry is visible again.
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "profileA-db")
        with ra._lock:
            head_a, total_a = ra.pending_head_for_session_locked(parent)
        assert head_a is not None and total_a == 1
    finally:
        monkeypatch.setattr(ra, "_child_provenance_current", ra._child_provenance_current.__wrapped__ if hasattr(ra._child_provenance_current, "__wrapped__") else ra._child_provenance_current)
        _clear(parent, child_key)


# ---------------------------------------------------------------------------
# MUST-FIX 3 (SILENT) — malformed model_config fails closed
# ---------------------------------------------------------------------------

def test_malformed_model_config_fails_closed(tmp_path, monkeypatch):
    """Malformed model_config must never fall through to the physical parent."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, model_config TEXT, source TEXT)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            ("malformed-child", "physical-parent", "{not-valid-json", "subagent"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(ra, "_child_parent_cache_key", lambda child: ("test-malformed-db", child))
    from api import models as api_models
    monkeypatch.setattr(api_models, "_active_state_db_path", lambda: str(db_path))
    with ra._lock:
        ra._child_approval_parents.clear()
    try:
        parent = ra._child_parent_session_id("malformed-child")
        assert parent is None, (
            "malformed model_config must fail closed (no physical-parent fallback)"
        )
    finally:
        with ra._lock:
            ra._child_approval_parents.clear()


def test_valid_delegate_from_still_resolves(tmp_path, monkeypatch):
    """A well-formed _delegate_from must still win over the physical parent."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, model_config TEXT, source TEXT)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            ("good-child", "physical-parent", json.dumps({"_delegate_from": "logical-parent"}), "subagent"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(ra, "_child_parent_cache_key", lambda child: ("test-good-db", child))
    from api import models as api_models
    monkeypatch.setattr(api_models, "_active_state_db_path", lambda: str(db_path))
    with ra._lock:
        ra._child_approval_parents.clear()
    try:
        parent = ra._child_parent_session_id("good-child")
        assert parent == "logical-parent"
    finally:
        with ra._lock:
            ra._child_approval_parents.clear()
