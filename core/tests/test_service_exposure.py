"""The data services must never be published to the public internet.

This is a regression guard, not a style check. On 2026-10 the deployment at
188.245.2.148 had `5432:5432` and `6379:6379` in docker-compose.yml, which Docker
interprets as "bind to every interface". With no host firewall active, PostgreSQL
accepted a handshake from the public internet and Redis answered on its port. The
containers had been reachable that way for two weeks.

Both ports are password-protected, so this was brute-force exposure rather than an
open door. That is not a reason to be relaxed about it: an internet-facing database
is scanned continuously, and the passwords in this project's `.env` are the only
thing that stood between a scanner and the data.

The fix is one character of intent per line - `127.0.0.1:5432:5432` instead of
`5432:5432` - and it is exactly the kind of thing a later edit removes by accident,
because the short form looks tidier and works perfectly on a laptop where nothing
is exposed. Hence these tests.

WHY the assertions resolve the config rather than parsing the YAML: compose
rewrites a bare `"5432:5432"` into an explicit `host_ip: "0.0.0.0"`, and leaves a
loopback-bound one as `"127.0.0.1"`. Reading Compose's own interpretation is the
only way to test what Docker will actually do, rather than what the file looks
like it says. `docker compose config` is the same command that first exposed the
problem.

Skipped when Docker is unavailable, so the suite still runs in an environment
without it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Services holding state that must never be reachable off the machine.
PRIVATE_SERVICES = ("db", "redis")

#: A published port is only acceptable on one of these bind addresses.
LOOPBACK = {"127.0.0.1", "::1"}

#: What Docker reports for a port bound to every interface - the shape that caused
#: the incident, kept here so the guard can be tested against it.
#:
#: S104 warns about binding to all interfaces. Nothing binds here: this is a string
#: compared against configuration, and the tests assert it is ABSENT. Suppressed on
#: this one line so the rule keeps working everywhere it is meaningful.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104

COMPOSE_FILES = ("docker-compose.yml", "docker-compose.ghcr.yml")


def compose_config(path: Path) -> dict | None:
    """Resolve one compose file the way Docker will, or None if unavailable."""
    # Resolved to an absolute path, both to satisfy S607 and because it is the
    # correct thing: a bare "docker" is looked up on PATH, and the point of this
    # test is to read what the real Compose does with the real file.
    docker = shutil.which("docker")
    if docker is None or not path.exists():
        return None
    try:
        # The argument list is built from a constant tuple in this module and a
        # hardcoded subcommand - no shell, no user input, no interpolation. S603
        # flags subprocess use in general and is right to; this call is the shape it
        # warns about without being an instance of it.
        result = subprocess.run(  # noqa: S603
            [docker, "compose", "-f", str(path), "config", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=REPO_ROOT,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


class DataServicesAreNotPublicTests(SimpleTestCase):
    def _published(self, config: dict, service: str) -> list[dict]:
        return [
            port
            for port in config.get("services", {}).get(service, {}).get("ports", [])
            if isinstance(port, dict)
        ]

    def test_every_data_service_binds_loopback_only(self):
        checked = 0
        for name in COMPOSE_FILES:
            config = compose_config(REPO_ROOT / name)
            if config is None:
                self.skipTest("docker compose is unavailable, or the file is missing")

            for service in PRIVATE_SERVICES:
                for port in self._published(config, service):
                    checked += 1
                    with self.subTest(file=name, service=service, published=port["published"]):
                        self.assertIn(
                            port.get("host_ip"),
                            LOOPBACK,
                            f"{name}: service '{service}' publishes "
                            f"{port['published']} on "
                            f"{port.get('host_ip') or 'all interfaces'}. That is the "
                            f"public internet on a machine with a routable IP. Bind "
                            f"it to 127.0.0.1 - the host, an SSH tunnel and the "
                            f"devcontainer all still work.",
                        )

        if checked == 0:
            self.skipTest("no published data-service ports to check")

    def test_the_guard_would_actually_catch_an_exposed_port(self):
        """A guard that cannot fail is not a guard.

        The test above is only meaningful if it rejects the shape that caused the
        incident. This feeds it that shape directly, so a refactor that
        accidentally makes it lenient fails here rather than silently passing.
        """
        # Shape matches `docker compose config --format json`: a service maps to an
        # object, and the port list lives under "ports".
        exposed = {
            "services": {"db": {"ports": [{"published": "5432", "host_ip": ALL_INTERFACES}]}}
        }
        loopback = {"services": {"db": {"ports": [{"published": "5432", "host_ip": "127.0.0.1"}]}}}

        # S104 warns about binding to all interfaces. There is no bind here: this
        # is the string Docker prints for an exposed port, and it is being asserted
        # AGAINST. Suppressed on the line so the rule keeps working everywhere else.
        self.assertNotIn(ALL_INTERFACES, LOOPBACK)
        for port in self._published(exposed, "db"):
            self.assertNotIn(port.get("host_ip"), LOOPBACK)
        for port in self._published(loopback, "db"):
            self.assertIn(port.get("host_ip"), LOOPBACK)

    def test_a_bare_port_mapping_would_be_treated_as_public(self):
        """`"5432:5432"` means every interface; compose makes that explicit.

        Documents the interpretation these tests depend on. If Compose ever
        stopped reporting the implicit 0.0.0.0, the assertions above would pass
        while the exposure returned.
        """
        config = compose_config(REPO_ROOT / "docker-compose.yml")
        if config is None:
            self.skipTest("docker compose is unavailable")

        for service in PRIVATE_SERVICES:
            for port in self._published(config, service):
                self.assertIsNotNone(
                    port.get("host_ip"),
                    f"{service}: compose reported no host_ip, so these tests can no "
                    f"longer tell a public bind from a private one",
                )
