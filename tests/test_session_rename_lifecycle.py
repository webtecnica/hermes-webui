"""Structural regressions for sidebar session inline rename (#1153).

The first Enter after double-click rename could appear to revert because the
frontend cleared `_renamingSid` before `/api/session/rename` completed. That
let normal session-list refreshes re-render stale cached data and destroy the
input while the save was still in flight.
"""

import pathlib


REPO = pathlib.Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _session_rename_block():
    start = SESSIONS_JS.find("function _buildSessionRenameStarter(")
    assert start >= 0, "shared session rename helper not found"
    end = SESSIONS_JS.find("function _appendSessionCopyLinkAction(", start)
    assert end > start, "session inline rename block end marker not found"
    return SESSIONS_JS[start:end]


def test_session_rename_finish_is_idempotent():
    block = _session_rename_block()
    assert "let finishDone=false;" in block, (
        "session rename finish() must track completion so Enter, blur, Escape, "
        "and delayed pointer paths cannot complete twice"
    )
    guard_pos = block.find("if(finishDone) return;")
    set_pos = block.find("finishDone=true;")
    assert guard_pos >= 0 and set_pos > guard_pos, (
        "session rename finish() must return early after the first completion"
    )


def test_session_rename_guard_releases_after_save_path_completes():
    block = _session_rename_block()
    assert block.count("_renamingSid=null;") == 1, (
        "_renamingSid must be cleared from one release helper only, not at the "
        "top of finish() before the async rename save settles"
    )
    release_pos = block.find("const releaseRename=()=>{")
    clear_pos = block.find("_renamingSid=null;")
    assert release_pos >= 0 and clear_pos > release_pos, (
        "_renamingSid should be cleared inside releaseRename(), after the "
        "selected finish path has completed"
    )
    api_pos = block.find("await api('/api/session/rename'")
    finally_pos = block.find("finally{")
    release_call_pos = block.find("releaseRename();", finally_pos)
    assert api_pos >= 0 and finally_pos > api_pos and release_call_pos > finally_pos, (
        "save path must await /api/session/rename before releaseRename() clears "
        "the render guard"
    )


def test_session_rename_completion_paints_while_keeping_a_newer_picker():
    block = _session_rename_block()
    release_pos = block.index("const releaseRename=()=>{")
    timer_pos = block.index("setTimeout", release_pos)
    timer_end = block.index("};", timer_pos)
    timer = block[timer_pos:timer_end]
    assert "_repaintSidebarKeepingProjectPicker()" in timer, (
        "a held rename response can finish after another row opens its picker; "
        "the delayed completion must paint now while keeping that picker"
    )


def test_session_rename_success_updates_cache_and_active_session_title():
    block = _session_rename_block()
    assert "_allSessions.find(item=>item&&item.session_id===session.session_id)" in block
    assert "target.display_title=nextTitle;" in block
    assert "target._state_db_title=nextTitle;" in block
    assert "if(S.session&&S.session.session_id===session.session_id){applyLocalTitle(S.session, nextTitle);syncTopbar();}" in block, (
        "successful session rename must keep cached and active titles coherent"
    )


def test_session_rename_failure_restores_state_and_surfaces_error():
    block = _session_rename_block()
    catch_pos = block.find("}catch(err){")
    assert catch_pos >= 0, "session rename save path must handle API failures"
    catch_block = block[catch_pos:block.find("}finally{", catch_pos)]
    assert "applyTitle(oldTitle,false);" in catch_block, (
        "failed session rename must restore the cached/active title instead of "
        "silently appearing successful"
    )
    assert "setStatus(msg);" in catch_block
    assert "showToast(msg,3000,'error')" in catch_block


def test_session_rename_blur_commits_current_title():
    block = _session_rename_block()
    assert "inp.onblur=()=>{ if(_renamingSid===session.session_id) finish(true); };" in block, (
        "blur should commit the edited title so rename matches the long-standing "
        "top-level session behavior"
    )


# ---------------------------------------------------------------------------
# #8503: Sidebar freezes when rename starts on a detached row
# ---------------------------------------------------------------------------


def test_session_rename_starter_guards_detached_display_element():
    """#8503: starting a rename on a detached row must return before setting _renamingSid."""
    block = _session_rename_block()
    guard_pos = block.find("if(!displayEl||!displayEl.isConnected) return;")
    set_pos = block.find("_renamingSid=session.session_id;")
    assert guard_pos >= 0, (
        "_buildSessionRenameStarter must check displayEl.isConnected to prevent "
        "detached rows from capturing global rename state"
    )
    assert guard_pos < set_pos, (
        "isConnected check must occur BEFORE setting _renamingSid"
    )


def test_session_row_dblclick_guards_detached_element():
    """#8503: double click on a detached row element must bail before calling startRename."""
    start = SESSIONS_JS.find("el.ondblclick=(e)=>{")
    assert start >= 0
    end = SESSIONS_JS.find("el.addEventListener('touchstart'", start)
    dblclick_block = SESSIONS_JS[start:end]
    assert "if (!el.isConnected) return;" in dblclick_block


def test_session_rename_starter_detached_row_simulation():
    """#8503: behavioral Node.js simulation: invoking rename starter on detached element leaves _renamingSid null."""
    import subprocess
    block = _session_rename_block()
    script = (
        "let _renamingSid = null;\n"
        "let _loadingSessionId = null;\n"
        "function _isReadOnlySession() { return false; }\n"
        "function closeSessionActionMenu() {}\n"
        "function _sessionDisplayTitle() { return 'Test'; }\n"
        "function _markNonCredentialInput() {}\n"
        "const S = {};\n"
        "const _allSessions = [];\n"
        + block + "\n"
        "const detachedEl = { isConnected: false };\n"
        "const starter1 = _buildSessionRenameStarter({ session_id: 'sid-1' }, detachedEl, () => {});\n"
        "starter1();\n"
        "if (_renamingSid !== null) throw new Error('_renamingSid was set for detached element: ' + _renamingSid);\n"
        "let replacedWith = null;\n"
        "const connectedEl = { isConnected: true, replaceWith: (inp) => { replacedWith = inp; } };\n"
        "global.document = { createElement: () => ({ addEventListener: () => {}, focus: () => {}, select: () => {} }) };\n"
        "global.window = {};\n"
        "global.setTimeout = () => {};\n"
        "const starter2 = _buildSessionRenameStarter({ session_id: 'sid-2' }, connectedEl, () => {});\n"
        "starter2();\n"
        "if (_renamingSid !== 'sid-2') throw new Error('Expected _renamingSid sid-2, got: ' + _renamingSid);\n"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert proc.returncode == 0

