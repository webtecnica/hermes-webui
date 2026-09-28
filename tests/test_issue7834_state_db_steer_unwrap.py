"""Tests for state.db-owned steer row unwrapping (#7834).

Typed steer rows in state.db (role='user', display_kind='steer') carry transport-level
[OUT-OF-BAND USER MESSAGE] markers that must be unwrapped when projected into WebUI
messages, while preserving byte-for-byte fidelity on untyped user rows, tool rows, and
malformed/nested frames.
"""
from __future__ import annotations

import sqlite3
import pytest

from api.models import (
    _project_state_db_message,
    get_state_db_session_messages,
    get_state_db_regeneration_tail_snapshot,
)

OOB_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once "
    "at this position; not tool output and not a new delivery when replayed from "
    "conversation history]"
)
OOB_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
OOB_BLOCK = f"{OOB_OPEN}\nsteer: use the staging bucket this time\n{OOB_CLOSE}"


def test_state_db_projection_unwraps_typed_steer_row():
    """A typed steer row (role='user', display_kind='steer') is unwrapped in the projection."""
    row = {
        "role": "user",
        "content": OOB_BLOCK,
        "timestamp": 1000.0,
        "display_kind": "steer",
    }
    msg = _project_state_db_message(
        row,
        available={"role", "content", "timestamp", "display_kind"},
        id_col=False,
        optional=["display_kind"],
    )
    assert msg["content"] == "steer: use the staging bucket this time"
    assert msg["display_kind"] == "steer"
    assert msg["role"] == "user"


def test_state_db_projection_leaves_untyped_user_row_intact():
    """An untyped user row quoting the marker is preserved byte-for-byte."""
    row = {
        "role": "user",
        "content": OOB_BLOCK,
        "timestamp": 1000.0,
    }
    msg = _project_state_db_message(
        row,
        available={"role", "content", "timestamp"},
        id_col=False,
        optional=["display_kind"],
    )
    assert msg["content"] == OOB_BLOCK


def test_state_db_projection_leaves_tool_row_intact():
    """A tool row with display_kind='steer' is not unwrapped."""
    row = {
        "role": "tool",
        "content": OOB_BLOCK,
        "timestamp": 1000.0,
        "display_kind": "steer",
        "tool_call_id": "call_1",
    }
    msg = _project_state_db_message(
        row,
        available={"role", "content", "timestamp", "display_kind", "tool_call_id"},
        id_col=False,
        optional=["display_kind", "tool_call_id"],
    )
    assert msg["content"] == OOB_BLOCK


def test_state_db_projection_preserves_malformed_frames():
    """Nested or malformed OOB markers are preserved byte-for-byte."""
    malformed = f"{OOB_OPEN}\n{OOB_BLOCK}\n{OOB_CLOSE}"
    row = {
        "role": "user",
        "content": malformed,
        "timestamp": 1000.0,
        "display_kind": "steer",
    }
    msg = _project_state_db_message(
        row,
        available={"role", "content", "timestamp", "display_kind"},
        id_col=False,
        optional=["display_kind"],
    )
    assert msg["content"] == malformed


def test_get_state_db_session_messages_unwraps_steer(tmp_path, monkeypatch):
    """get_state_db_session_messages unwraps typed steer rows from a real state.db."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL,
            display_kind TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?, ?, ?, ?, ?)",
        ("sid-1", "user", "initial prompt", 100.0, None),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?, ?, ?, ?, ?)",
        ("sid-1", "user", OOB_BLOCK, 101.0, "steer"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    msgs = get_state_db_session_messages("sid-1")
    assert len(msgs) == 2
    assert msgs[0]["content"] == "initial prompt"
    assert msgs[1]["content"] == "steer: use the staging bucket this time"
    assert msgs[1]["display_kind"] == "steer"


def test_get_state_db_session_snapshot_unwraps_prefix_and_tail(tmp_path, monkeypatch):
    """get_state_db_session_snapshot unwraps steer rows in both prefix keys and tail rows."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL,
            display_kind TEXT
        )
        """
    )
    # Prefix steer (< 200.0)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?, ?, ?, ?, ?)",
        ("sid-2", "user", OOB_BLOCK, 150.0, "steer"),
    )
    # Tail steer (>= 200.0)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?, ?, ?, ?, ?)",
        ("sid-2", "user", OOB_BLOCK, 250.0, "steer"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    snapshot = get_state_db_regeneration_tail_snapshot("sid-2", 200.0)
    assert snapshot is not None
    assert len(snapshot["prefix_keys"]) == 1
    # The prefix key content should be the unwrapped text, not the OOB wrapper
    assert "OUT-OF-BAND" not in str(snapshot["prefix_keys"][0])
    assert "use the staging bucket this time" in str(snapshot["prefix_keys"][0])

    assert len(snapshot["tail"]) == 1
    assert snapshot["tail"][0]["content"] == "steer: use the staging bucket this time"
    assert snapshot["tail"][0]["display_kind"] == "steer"
