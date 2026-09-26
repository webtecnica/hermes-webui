"""Regression tests for issue #7348: Korean IME Enter sends message directly on first press.

Verifies:
1. Backend config default & boolean validation for `ime_enter_sends`.
2. Frontend boot.js initialization and `_isImeEnter` bypass when `_imeEnterSends` is enabled.
3. Frontend boot.js keyup unstick logic so `_imeComposing` cannot stay stuck.
4. Settings UI, preferences payload, autosave wiring, and i18n translations.
"""

from pathlib import Path
import re
import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_config_ime_enter_sends_defaults_and_validation():
    from api import config
    assert "ime_enter_sends" in config._SETTINGS_DEFAULTS
    assert config._SETTINGS_DEFAULTS["ime_enter_sends"] is False
    assert "ime_enter_sends" in config._SETTINGS_BOOL_KEYS


def test_boot_js_ime_enter_sends_bypass():
    """_isImeEnter must check window._imeEnterSends and return false when active."""
    pattern = re.compile(
        r"function\s+_isImeEnter\s*\(\s*e\s*\)\s*\{[^}]*"
        r"if\s*\(\s*window\._imeEnterSends\s*\)\s*return\s+false\s*;?[^}]*"
        r"e\.isComposing[^}]*"
        r"e\.keyCode\s*===\s*229[^}]*"
        r"_imeComposing[^}]*\}",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "_isImeEnter must check window._imeEnterSends and return false to allow Korean IME send"
    )


def test_boot_js_keyup_unsticks_ime_composing():
    """keyup listener on #msg must clear _imeComposing if non-composing key is released."""
    pattern = re.compile(
        r"keyup['\"]\s*,\s*e\s*=>\s*\{[^}]*"
        r"!e\.isComposing\s*&&\s*e\.keyCode\s*!==\s*229[^}]*"
        r"_imeComposing\s*=\s*false",
        re.DOTALL,
    )
    assert pattern.search(BOOT_JS), (
        "keyup listener on #msg must clear _imeComposing when key is not composing"
    )


def test_boot_js_localstorage_and_settings_initialization():
    """window._imeEnterSends must be populated from localStorage synchronously and /api/settings."""
    assert "localStorage.getItem('hermes-pref-ime_enter_sends')" in BOOT_JS
    assert "window._imeEnterSends=!!s.ime_enter_sends;" in BOOT_JS


def test_index_html_has_ime_enter_sends_setting():
    """index.html must have the settingsImeEnterSends checkbox."""
    assert 'id="settingsImeEnterSends"' in INDEX_HTML
    assert 'data-i18n="settings_label_ime_enter_sends"' in INDEX_HTML


def test_panels_js_preference_handling():
    """panels.js must extract, save, and bind settingsImeEnterSends."""
    assert "settingsImeEnterSends" in PANELS_JS
    assert "payload.ime_enter_sends=" in PANELS_JS
    assert "localStorage.setItem('hermes-pref-ime_enter_sends'" in PANELS_JS
    assert "body.ime_enter_sends=" in PANELS_JS


def test_i18n_has_ime_enter_sends_keys():
    """i18n.js must include settings_label_ime_enter_sends in en and ko."""
    assert "settings_label_ime_enter_sends" in I18N_JS
    assert "settings_desc_ime_enter_sends" in I18N_JS
    assert "IME 입력 시 Enter로 전송" in I18N_JS
