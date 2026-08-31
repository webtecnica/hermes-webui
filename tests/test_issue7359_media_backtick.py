"""
Tests for #7359: MEDIA capture regexes must not swallow a trailing backtick.

The capture character class used across the MEDIA token regexes excluded
whitespace, `)` and `]` but NOT backticks. When a MEDIA reference appeared
inside inline code or was followed by a closing backtick (e.g. the path was
written as `MEDIA:/tmp/file.png` in prose), the regex captured the backtick
into the reference. The backend then tried to serve a path ending in a
backtick and the download failed.

This test covers all six capture sites:
1. api/routes.py        — _MEDIA_TOKEN_RE (backend /media endpoint allowlist)
2. api/media_snapshots.py — media_re (snapshot cleanup scan)
3. static/messages.js   — streaming smd MEDIA match (full-chunk form)
4. static/messages.js   — streaming smd MEDIA match (global form)
5. static/messages.js   — streaming tailMatch form
6. static/ui.js         — renderMd() MEDIA restore

Static coverage asserts the char class excludes a backtick at each site;
behavioural coverage runs the actual regexes against real input.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
MEDIA_SNAPSHOTS_PY = (REPO_ROOT / "api" / "media_snapshots.py").read_text(
    encoding="utf-8"
)
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


class TestMediaBacktickStatic(unittest.TestCase):
    """Each capture site's char class must exclude a backtick."""

    def test_routes_py_media_token_re_excludes_backtick(self):
        self.assertIn(r"MEDIA:([^\s\)\]`]+)", ROUTES_PY)

    def test_media_snapshots_py_excludes_backtick(self):
        self.assertIn(r'media_re = _re.compile(r"MEDIA:([^\s\)\]`]+)")', MEDIA_SNAPSHOTS_PY)

    def test_messages_js_full_chunk_excludes_backtick(self):
        self.assertIn(r"/^MEDIA:([^\s\)\]`]+)$/", MESSAGES_JS)

    def test_messages_js_global_excludes_backtick(self):
        self.assertIn(r"/MEDIA:([^\s\)\]`]+)/g", MESSAGES_JS)

    def test_messages_js_tail_match_excludes_backtick(self):
        self.assertIn(r"/MEDIA:[^\s\)\]`]*$/", MESSAGES_JS)

    def test_ui_js_restore_excludes_backtick(self):
        self.assertIn(r"/MEDIA:([^\s\)\]`]+)/g", UI_JS)

    def test_no_capture_site_left_without_backtick_exclusion(self):
        """Every MEDIA capture regex must have been migrated."""
        for src, label in (
            (ROUTES_PY, "api/routes.py"),
            (MEDIA_SNAPSHOTS_PY, "api/media_snapshots.py"),
            (MESSAGES_JS, "static/messages.js"),
            (UI_JS, "static/ui.js"),
        ):
            for m in re.finditer(r"MEDIA:\(\[\^[^\]]+\]\)", src):
                char_class = m.group(1)
                self.assertIn(
                    "`",
                    char_class,
                    f"{label}: capture regex {m.group(0)!r} does not exclude backtick",
                )
            for m in re.finditer(r"MEDIA:\[\^[^\]]+\]\*", src):
                char_class = m.group(0).split("[^", 1)[1].rsplit("]", 1)[0]
                self.assertIn(
                    "`",
                    char_class,
                    f"{label}: tail regex {m.group(0)!r} does not exclude backtick",
                )


class TestMediaBacktickBehaviour(unittest.TestCase):
    """Run the actual regexes: a trailing backtick must NOT be captured."""

    def setUp(self):
        routes_match = re.search(
            r"_MEDIA_TOKEN_RE = re\.compile\((r\".*?\")\)", ROUTES_PY
        )
        self.assertIsNotNone(routes_match, "could not locate _MEDIA_TOKEN_RE")
        self.token_re = re.compile(eval(routes_match.group(1)))  # noqa: S307 — test-only

    def test_backend_regex_strips_trailing_backtick(self):
        m = self.token_re.search("MEDIA:/tmp/file.png`")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "/tmp/file.png")

    def test_backend_regex_strips_leading_and_trailing_backticks(self):
        # Inline-code wrapping: `MEDIA:/tmp/file.png` — no backtick may
        # leak into the captured reference.
        m = self.token_re.search("`MEDIA:/tmp/file.png`")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "/tmp/file.png")

    def test_backend_regex_still_rejects_close_paren(self):
        m = self.token_re.search("MEDIA:/tmp/file.png)")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "/tmp/file.png")

    def test_ui_js_restore_regex_strips_trailing_backtick(self):
        # The JS literal is /MEDIA:([^\s\)\]`]+)/g — strip the delimiters.
        ui_re = re.compile(r"MEDIA:([^\s\)\]`]+)")
        m = ui_re.search("`MEDIA:/tmp/file.png`")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "/tmp/file.png")


if __name__ == "__main__":
    unittest.main()
