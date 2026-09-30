"""Secret-hygiene guards.

These tests exist so that a credential can never be re-introduced into a
committed file without CI failing. They are deliberately written to construct
the forbidden literals at runtime, so this module never contains the very
strings it searches for.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.settings import env_required

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Directories that are never published to the repository.
_SKIP_DIRS = {
    "venv",
    ".venv",
    ".runtime",
    ".git",
    "__pycache__",
    "logs",
    "staticfiles",
    "media",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}

# Files that are *expected* to hold real secrets (gitignored, never committed).
_SKIP_FILES = {".env", "celerybeat-schedule"}

_SKIP_SUFFIXES = {".log", ".pyc", ".sqlite3", ".gz", ".png", ".jpg", ".whl"}

# Built at runtime so this file does not match its own scan.
_LEAKED_DB_PASSWORD = "alpha_secure_" + "password_2026"
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-" + r"api\d{2}-[A-Za-z0-9_\-]{24,}")
_OPENAI_KEY_RE = re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{32,}\b")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def _iter_committed_files():
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name in _SKIP_FILES or Path(name).suffix in _SKIP_SUFFIXES:
                continue
            yield Path(root) / name


class EnvRequiredTests(SimpleTestCase):
    def test_missing_secret_raises_with_actionable_message(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(ImproperlyConfigured) as ctx,
        ):
            env_required("SOME_MISSING_SECRET")
        message = str(ctx.exception)
        self.assertIn("SOME_MISSING_SECRET", message)
        self.assertIn(".env.example", message)

    def test_blank_secret_is_treated_as_missing(self):
        with (
            patch.dict(os.environ, {"SOME_SECRET": "   "}, clear=True),
            self.assertRaises(ImproperlyConfigured),
        ):
            env_required("SOME_SECRET")

    def test_present_secret_is_returned_stripped(self):
        with patch.dict(os.environ, {"SOME_SECRET": "  value  "}, clear=True):
            self.assertEqual(env_required("SOME_SECRET"), "value")


class SettingsSourceTests(SimpleTestCase):
    """Settings must not ship credential fallbacks."""

    def setUp(self):
        self.source = (BASE_DIR / "config" / "settings.py").read_text(encoding="utf-8")

    def test_no_hardcoded_database_password(self):
        self.assertNotIn(_LEAKED_DB_PASSWORD, self.source)
        self.assertNotIn("alpha_secure", self.source)

    def test_no_hardcoded_secret_key_fallback(self):
        # A default SECRET_KEY would silently ship to production.
        self.assertNotIn("dev-only-insecure", self.source)
        self.assertNotIn("change-me", self.source)

    def test_database_password_is_required_from_the_environment(self):
        self.assertIn('env_required(\n            "POSTGRES_PASSWORD"', self.source)


class RepositorySecretScanTests(SimpleTestCase):
    """Scan every file that would be published to GitHub."""

    def test_no_credentials_are_committed(self):
        findings: list[str] = []

        for path in _iter_committed_files():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            relative = path.relative_to(BASE_DIR)
            if _LEAKED_DB_PASSWORD in text:
                findings.append(f"{relative}: hard-coded database password")
            for label, pattern in (
                ("Anthropic API key", _ANTHROPIC_KEY_RE),
                ("OpenAI-style API key", _OPENAI_KEY_RE),
                ("AWS access key id", _AWS_KEY_RE),
                ("private key block", _PRIVATE_KEY_RE),
            ):
                if pattern.search(text):
                    findings.append(f"{relative}: {label}")

        self.assertEqual(
            findings, [], "Potential secrets in committed files:\n" + "\n".join(findings)
        )

    def test_env_file_is_gitignored(self):
        ignore = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", ignore.splitlines(), "`.env` must be listed in .gitignore")

    def test_env_example_contains_no_real_values(self):
        example = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn(_LEAKED_DB_PASSWORD, example)
        self.assertNotRegex(example, _ANTHROPIC_KEY_RE)
        # Placeholders, not working credentials.
        self.assertIn("replace-me", example)
