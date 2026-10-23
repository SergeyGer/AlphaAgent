"""Every source link in the documentation must point at a real file and line.

Two separate failures motivated this, both of which shipped.

**Wrong link form.** `LLM-Quality-and-Guardrails.md` originally linked to source as
`../blob/main/ai_agent.py#L87`. That reads correctly when the page is rendered from
`docs/wiki/` inside the main repository, and is wrong everywhere else: the wiki is a
*separate git repository* (`AlphaAgent.wiki.git`), so the relative path resolves to
`github.com/SergeyGer/AlphaAgent.wiki/blob/main/ai_agent.py` — a 404. Verified:
the relative form returns 404, the absolute form returns 200. Documentation is
mostly read on the wiki, so the broken form was the one nearly everyone would hit.

**Stale line numbers.** An absolute URL survives the wiki move but not a refactor.
`#L87` points at whatever is on line 87 today, and nothing notices when that stops
being `class TradeProposal`. A link that lands on the wrong function is worse than
a dead link, because it looks like it worked.

`lychee` catches neither. It checks HTTP status, so it passes a URL whose line
number is wrong, and the repository's own lychee config excludes
`github.com/.../blob/` because that route intermittently returns 503 — which is why
these links are checked here instead, against the files on disk.

The check is deliberately offline: it reads the local checkout, so it pins the
*file and line*, not just reachability.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC_DIRS = (REPO_ROOT / "docs", REPO_ROOT / "README.md")

#: Absolute blob URLs pointing at this repository, with an optional line anchor.
BLOB_URL = re.compile(
    r"https://github\.com/SergeyGer/AlphaAgent/blob/main/"
    # Greedy, not lazy. With a LAZY path and an OPTIONAL line anchor the regex
    # matches a single character and reports path='a', path='s' - 26 bogus
    # failures. Greedy stops correctly at '#' because '#' is not in the class.
    r"(?P<path>[\w./-]+)"
    r"(?:#L(?P<line>\d+))?"
)

#: Line anchors that are allowed to point past the end of the file. None: if a
#: link names a line, that line must exist.
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".yml", ".yaml", ".json", ".toml", ".md"}


def document_files() -> list[Path]:
    files = [REPO_ROOT / "README.md"]
    files.extend(sorted((REPO_ROOT / "docs").rglob("*.md")))
    return [f for f in files if f.exists()]


class DocumentationSourceLinksTests(SimpleTestCase):
    def _links(self):
        for doc in document_files():
            for match in BLOB_URL.finditer(doc.read_text(encoding="utf-8")):
                yield doc, match

    def test_every_source_link_points_at_a_file_that_exists(self):
        checked = 0
        for doc, match in self._links():
            checked += 1
            target = REPO_ROOT / match.group("path")
            with self.subTest(doc=doc.name, path=match.group("path")):
                self.assertTrue(
                    target.exists(),
                    f"{doc.relative_to(REPO_ROOT)} links to "
                    f"{match.group('path')}, which does not exist",
                )
        if checked == 0:
            self.skipTest("no source links to check")

    def test_every_line_anchor_lands_inside_the_file(self):
        """A line past the end is a refactor that nothing else would catch."""
        checked = 0
        for doc, match in self._links():
            if match.group("line") is None:
                continue
            checked += 1
            target = REPO_ROOT / match.group("path")
            if not target.exists():
                continue  # reported by the test above
            line = int(match.group("line"))
            total = len(target.read_text(encoding="utf-8").splitlines())
            with self.subTest(doc=doc.name, path=match.group("path"), line=line):
                self.assertLessEqual(
                    line,
                    total,
                    f"{doc.relative_to(REPO_ROOT)} links to {match.group('path')}"
                    f"#L{line}, but that file has only {total} lines",
                )
        if checked == 0:
            self.skipTest("no line anchors to check")

    def test_no_document_uses_the_relative_wiki_breaking_form(self):
        """`../blob/...` resolves to the *wiki* repository, where it 404s.

        This is the regression guard for the specific mistake: it shipped in 18
        links and only a CI failure exposed it.
        """
        offenders = []
        for doc in document_files():
            text = doc.read_text(encoding="utf-8")
            if "](../blob/" in text or "](..\\blob\\" in text:
                offenders.append(str(doc.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [], f"relative blob links found in: {offenders}")

    def test_the_guard_would_actually_catch_a_broken_link(self):
        """A check that cannot fail proves nothing."""
        real = "https://github.com/SergeyGer/AlphaAgent/blob/main/ai_agent.py#L87"
        fake = "https://github.com/SergeyGer/AlphaAgent/blob/main/does_not_exist.py#L1"

        m_real = BLOB_URL.search(real)
        m_fake = BLOB_URL.search(fake)
        self.assertIsNotNone(m_real)
        self.assertIsNotNone(m_fake)
        self.assertTrue((REPO_ROOT / m_real.group("path")).exists())
        self.assertFalse((REPO_ROOT / m_fake.group("path")).exists())

    def test_source_links_are_absolute_not_relative(self):
        """Absolute is the only form that works in both render targets."""
        for doc, match in self._links():
            with self.subTest(doc=doc.name):
                self.assertTrue(match.group(0).startswith("https://github.com/"))

    def test_the_documented_suffixes_are_real(self):
        """Keep SOURCE_SUFFIXES honest: it documents what the links may target."""
        for suffix in SOURCE_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertTrue(
                    any(REPO_ROOT.rglob(f"*{suffix}")),
                    f"SOURCE_SUFFIXES lists {suffix}, but no such file exists",
                )
