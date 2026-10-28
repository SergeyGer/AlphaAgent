"""The demo's theme is written against one Streamlit version, and says so.

`demo/app.py` styles itself by injecting CSS that targets Streamlit's own
`data-testid` attributes. Those are internal names: they are stable within a
release and not promised across one. A framework upgrade could therefore silently
untheme the page - it would still render, just wrong, and nothing would fail.

Pinning `streamlit==1.65.0` in `demo/requirements.txt` is the actual fix; this test
is what makes an upgrade **visible**. Bumping the pin without re-checking the
selectors now fails here with the reason, instead of shipping a demo that quietly
lost its appearance.

If the pin is deliberately raised: verify the theme against the new version, then
update `THEMED_AGAINST` here. That edit is the record that someone looked.
"""

from __future__ import annotations

import pathlib
import re

from django.test import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DEMO = REPO_ROOT / "demo" / "app.py"
REQUIREMENTS = REPO_ROOT / "demo" / "requirements.txt"

#: The Streamlit release the injected CSS was verified against.
THEMED_AGAINST = "1.65.0"


class DemoThemeContractTests(SimpleTestCase):
    def test_the_pinned_streamlit_is_the_one_the_theme_targets(self):
        pinned = re.search(r"^streamlit==([\w.]+)$", REQUIREMENTS.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(pinned, "demo/requirements.txt no longer pins streamlit exactly")
        self.assertEqual(
            pinned.group(1),
            THEMED_AGAINST,
            "demo/requirements.txt pins Streamlit "
            f"{pinned.group(1)}, but the theme in demo/app.py was written and "
            f"verified against {THEMED_AGAINST}. Streamlit's data-testid attributes "
            "are internal and may move between releases: re-check the CSS, then "
            "update THEMED_AGAINST in this file to record that someone did.",
        )

    def test_the_demo_still_uses_testid_selectors(self):
        """If the selectors were replaced with something stable, drop this contract."""
        css = DEMO.read_text(encoding="utf-8")
        self.assertGreater(
            css.count("data-testid"),
            0,
            "demo/app.py no longer targets data-testid selectors - if the theme now "
            "rests on documented API instead, delete THEMED_AGAINST and this test.",
        )

    def test_the_demo_does_not_import_the_framework_for_styling(self):
        """The theme must be CSS injection, not a plugin, or the pin means nothing."""
        text = DEMO.read_text(encoding="utf-8")
        self.assertIn("unsafe_allow_html=True", text, "the demo no longer injects CSS")
