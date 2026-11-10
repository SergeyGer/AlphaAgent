#!/usr/bin/env python
#
# RUF001/RUF003 flag characters that are confusable across scripts: a Cyrillic
# letter that reads as Latin "c". They are not silenced here. This file generates
# English documentation, so any Cyrillic in it is a defect that the linter should
# catch before a reader does.
"""Generate Run_Stop_Project.docx - the English-language operator guide.

Kept in the repository, unlike its output, so the document can be regenerated
when the commands change. The .docx itself is gitignored: it is a personal
reference for one operator rather than a project artefact, and a binary Word
rewrites on every open would add nothing to the history but noise.

    python scripts/make_run_stop_doc.py

The script self-checks before writing: it refuses to produce a document that
names an SSH key file the configured IdentityFile does not use. A guide that tells
you to load ~/.ssh/id_rsa when your key is id_ed25519 is worse than no guide,
because the failure looks like a broken server rather than a stale document.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
except ImportError:  # pragma: no cover
    sys.exit("python-docx is required: pip install python-docx")

OUTPUT = Path(__file__).resolve().parent.parent / "Run_Stop_Project.docx"

# ---------------------------------------------------------------------------
# Facts the document is built from.
#
# Constants rather than prose, because the document must not contradict itself and
# because the checks below reason about them.
# ---------------------------------------------------------------------------

SERVER_IP = "188.245.2.148"
SSH_HOST = "Hetzner"

#: How the Linux server is named throughout. The IP is repeated deliberately: the
#: reader works from several machines, and "the Linux server" stops being
#: unambiguous the moment there is more than one of anything.
SERVER = f"the Linux server (the live server at {SERVER_IP})"
SRV = f"the server at {SERVER_IP}"

#: The key file named in the reader's ~/.ssh/config. Every instruction uses this
#: name; SSH_KEY_ALIASES are the ones that must NOT appear anywhere.
SSH_KEY = "id_ed25519"
SSH_KEY_ALIASES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519_sk")

#: The reader's client-side SSH config, reproduced verbatim. It lives on the
#: LAPTOP, not on the server - the server has no such file.
SSH_CONFIG = f"""Host {SSH_HOST} {SERVER_IP}
    HostName {SERVER_IP}
    User root
    IdentityFile ~/.ssh/{SSH_KEY}
    IdentitiesOnly yes
    AddKeysToAgent yes
    ServerAliveInterval 30
    ServerAliveCountMax 4
    TCPKeepAlive yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PreferredAuthentications publickey"""

#: DeepSeek Harness runs as a systemd unit on the server, bound to loopback.
DSH_UNIT = "dsh-web"
DSH_PORT = 3080

ACCENT = RGBColor(0x1F, 0x3A, 0x5F)
MUTED = RGBColor(0x60, 0x60, 0x60)
DANGER = RGBColor(0xA6, 0x1B, 0x1B)

#: Filled by build() before the self-check runs.
DOCUMENT_TEXT = ""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def shade(cell, hex_fill: str) -> None:
    """Fill a table cell. python-docx has no API for this, so it is done in XML."""
    element = OxmlElement("w:shd")
    element.set(qn("w:val"), "clear")
    element.set(qn("w:fill"), hex_fill)
    cell._tc.get_or_add_tcPr().append(element)


def code(doc: Document, text: str) -> None:
    """A monospaced block, visually distinct from body text."""
    for line in text.strip("\n").split("\n"):
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(2)
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.left_indent = Pt(18)
        run = paragraph.add_run(line)
        run.font.name = "Consolas"
        run.font.size = Pt(9.5)
        run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)


def body(doc: Document, text: str, *, bold: bool = False, italic: bool = False) -> None:
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    if italic:
        run.font.color.rgb = MUTED


def warn(doc: Document, text: str) -> None:
    """A paragraph that must not be skimmed past."""
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(text)
    run.bold = True
    run.font.color.rgb = DANGER


def bullet(doc: Document, text: str) -> None:
    doc.add_paragraph(text, style="List Bullet")


def numbered(doc: Document, text: str) -> None:
    doc.add_paragraph(text, style="List Number")


def heading(doc: Document, text: str, level: int = 1) -> None:
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = ACCENT


def t(*lines: str) -> str:
    """Join wrapped prose into one table cell.

    Explicit rather than implicit. Two string literals abutting inside a list are
    indistinguishable from a missing comma - the second element silently joins the
    first, and nothing reports it - which is what CodeQL's
    py/implicit-string-concatenation-in-list is warning about. A call site makes
    the intent unambiguous to a reader as well as to a linter, and costs one
    wrapper per wrapped cell.
    """
    return " ".join(line.strip() for line in lines)


def table(doc: Document, headers: list[str], rows: list[list[str]]) -> None:
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, text in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        run.bold = True
        shade(cell, "DCE6F1")
    for row in rows:
        cells = t.add_row().cells
        for i, text in enumerate(row):
            cells[i].text = text
    doc.add_paragraph()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def intro(doc: Document) -> None:
    title = doc.add_heading("AlphaAgent — Operator Runbook", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    body(
        doc,
        "Everything you need to start work on the project, stop it, connect from a "
        "new laptop, and never leave open what should be closed.",
        italic=True,
    )
    table(
        doc,
        ["Section", "When you need it"],
        [
            ["1-4", "Day-to-day work: start, stop, open in the browser"],
            ["5", "First login from a new laptop: SSH key and setup"],
            ["6", "Connecting to DeepSeek Harness"],
            ["7-8", "Dev Containers, including two people on one server"],
            ["9-10", "Several laptops, cloning the repository"],
            ["11", "Security: fail2ban, ufw, secrets"],
            ["12-14", "Command reference, troubleshooting, cheat sheet"],
        ],
    )
    body(
        doc,
        f"Commands in this document are run either in a terminal on {SERVER}, or in "
        f"a VS Code terminal connected to it. Anything done on the laptop without a "
        f"connection to the server is called out separately.",
        italic=True,
    )


def section_quick(doc: Document) -> None:
    heading(doc, "1. The Short Version", 1)
    body(
        doc,
        f"The project consists of six containers and runs on {SERVER}. They are "
        f"started and stopped with a single command — there is no need to start each "
        f"one by hand.",
    )
    table(
        doc,
        ["Container", "What it is", "Port on the server"],
        [
            ["web", "Web server: API, control panel, WebSocket", "8080"],
            ["worker", "Celery: runs AI-agent tasks", "—"],
            ["beat", "Scheduler: runs scans on a schedule", "—"],
            ["mcp", "Tool server (quotes, news)", "—"],
            ["db", "PostgreSQL 16 — the database", "5432 (localhost only)"],
            ["redis", "Redis 7 — task queue and cache", "6379 (localhost only)"],
        ],
    )
    body(
        doc,
        f"Control panel once running: http://127.0.0.1:8080/ "
        f"(on {SRV}). Login demo, password demo-pass-123.",
    )
    warn(
        doc,
        "The databases are reachable only from the server itself. That is "
        "deliberate: PostgreSQL and Redis used to be exposed to the internet. See "
        "section 11 for details.",
    )


def section_run_stop(doc: Document) -> None:
    heading(doc, "2. Starting and Stopping", 1)

    heading(doc, "2.1. Starting", 2)
    body(doc, "Three ways. Pick whichever is convenient — the result is identical.")
    body(doc, "Method 1. From VS Code (identical on Windows and Linux):", bold=True)
    bullet(doc, "Ctrl+Shift+P")
    bullet(doc, "type Run Task and select 'Tasks: Run Task'")
    bullet(doc, "select 'AlphaAgent: Start'")
    body(doc, "Method 2. In a terminal (Linux, macOS, WSL, Git Bash):", bold=True)
    code(doc, "make start")
    body(doc, "Method 3. Directly through Docker (works everywhere):", bold=True)
    code(doc, "docker compose up -d --build")
    body(
        doc,
        "The first start takes a few minutes: Docker builds the image. Subsequent "
        "starts take a few seconds.",
    )

    heading(doc, "2.2. Stopping — The Most Important Section", 2)
    body(
        doc,
        "There are two commands, and they do different things. Your choice decides "
        "whether services from different projects will get in each other's way.",
    )
    table(
        doc,
        ["Command", "What happens", "When to use it"],
        [
            [
                "make stop\n('AlphaAgent: Stop')",
                "Containers are stopped but stay built. Ports and memory are released.",
                "A short break. Starting again afterwards is instant.",
            ],
            [
                "make down\n('AlphaAgent: Shut down')",
                "Containers are removed and the network is fully released.",
                t(
                    "Switching to another project for a long time. This is what ",
                    "actually frees ports 8080, 5432 and 6379.",
                ),
            ],
        ],
    )
    warn(
        doc,
        "If you are switching to another project, use down. Under some Docker "
        "Desktop configurations, stopped containers keep holding their ports, and "
        "then the second project cannot start.",
    )
    body(
        doc,
        "No data is lost: neither stop nor down deletes volumes. Only down -v "
        "deletes data, and that command is deliberately not wrapped in any task — "
        "so it cannot be run by a single typo.",
        italic=True,
    )

    heading(doc, "2.3. Switching Between Projects", 2)
    code(
        doc,
        """# finished with AlphaAgent
cd ~/projects/AlphaAgent
make down

# ... working on another project ...

# back again
cd ~/projects/AlphaAgent
make start""",
    )

    heading(doc, "2.4. Autostart Is Disabled", 2)
    body(
        doc,
        "No container starts on its own — neither after a server reboot nor after a "
        "Docker restart. That is deliberate:",
    )
    bullet(doc, "the project occupies no memory or ports while you are not working on it;")
    bullet(doc, "autonomous trading does not resume without your knowledge;")
    bullet(doc, "services from different projects do not compete for the same ports.")
    body(doc, "To check, the answer must be no for every container:")
    code(doc, "docker inspect alpha_web --format '{{.HostConfig.RestartPolicy.Name}}'")
    body(
        doc,
        "The exception is DeepSeek Harness: it runs as a system service and comes "
        "back up by itself after a reboot. It is a separate service, see section 6.",
        italic=True,
    )


def section_ports(doc: Document) -> None:
    heading(doc, "3. Which Port to Open", 1)
    body(
        doc,
        "Two paths lead to the same application, and their port numbers differ. "
        "This is the most common mistake: the application is running, but the "
        "browser says the site cannot be reached.",
    )
    table(
        doc,
        ["Where you are", "How the traffic flows", "Address in the browser"],
        [
            [
                "In VS Code, inside the Dev Container",
                "VS Code forwards port 8000 of the container to your laptop",
                "http://127.0.0.1:8000/",
            ],
            [
                f"On {SRV} (over SSH)",
                "Docker publishes port 8000 of the container as 8080 on the server",
                "http://127.0.0.1:8080/",
            ],
        ],
    )
    body(
        doc,
        "It is the same application. The numbers differ because these are two "
        "different mechanisms, not two different servers.",
    )
    warn(
        doc,
        "If you are working through Dev Containers, open port 8000. Port 8080 "
        "exists only on the server side; nothing listens on it inside the container, "
        "so forwarding it is pointless.",
    )
    body(
        doc,
        "To see which ports VS Code has already forwarded, open the PORTS tab — "
        "next to the TERMINAL tab at the bottom of the VS Code window.",
    )


def section_not_serving(doc: Document) -> None:
    heading(doc, "4. The Application Does Not Open Right After the Container Starts", 1)
    body(
        doc,
        "That is by design. In Dev Container mode the web container is kept alive "
        "specifically so VS Code can attach to it, but the web server itself is not "
        "started. Until you start it, there is nothing to open.",
    )
    body(doc, "Method 1 — through the VS Code task list:", bold=True)
    bullet(doc, "Ctrl+Shift+P, then 'Tasks: Run Task'")
    bullet(doc, "select 'AlphaAgent: Run server (in this container)'")
    body(doc, "Method 2 — in a terminal inside the container:", bold=True)
    code(doc, "python manage.py runserver 0.0.0.0:8000")
    warn(
        doc,
        "0.0.0.0 is mandatory. By default Django listens only on 127.0.0.1 — the "
        "container's own loopback. A forwarded port then connects into the void, and "
        "the browser shows an error even though the log says Starting development "
        "server.",
    )
    body(doc, "If there is no demo data yet, run this once:")
    code(
        doc,
        """python manage.py migrate
python manage.py seed_demo""",
    )
    body(
        doc,
        "Or select the 'AlphaAgent: Migrate + seed demo data' task — it does both.",
    )


def section_ssh(doc: Document) -> None:
    heading(doc, "5. Server Access: SSH Key from Scratch", 1)
    body(
        doc,
        f"All the sections below assume you can already log in to {SERVER} without "
        f"a password. If you are on a new laptop, start here.",
    )
    body(
        doc,
        "Password login is disabled on the server. That is correct and must stay "
        "that way: a password can be guessed, a key practically cannot.",
        italic=True,
    )

    heading(doc, "5.1. Creating a Key on the Laptop (Windows)", 2)
    body(
        doc,
        "Open PowerShell. The ssh-keygen command ships with Windows 10 and 11 "
        "starting from version 1809; nothing needs to be installed separately.",
    )
    code(
        doc,
        f'ssh-keygen -t ed25519 -C "your-laptop" -f $HOME\\.ssh\\{SSH_KEY}',
    )
    body(doc, "When asked about a passphrase:")
    bullet(doc, "set one — then a stolen key file is useless on its own;")
    bullet(
        doc,
        "or press Enter twice — more convenient, but the key file becomes the "
        "only protection. Acceptable for a personal laptop, not for a shared one.",
    )
    body(doc, "Two files will be created:")
    table(
        doc,
        ["File", "What it is", "Who may see it"],
        [
            [f"~/.ssh/{SSH_KEY}", "PRIVATE key", "Nobody. Ever."],
            [f"~/.ssh/{SSH_KEY}.pub", "Public key", "Safe to put on the server"],
        ],
    )
    body(doc, "On Linux and macOS the command is the same, except the path has no backslashes:")
    code(doc, f'ssh-keygen -t ed25519 -C "your-laptop" -f ~/.ssh/{SSH_KEY}')

    heading(doc, "5.2. Putting the Public Part on the Server", 2)
    body(
        doc,
        "Method 1 — automatic. The simplest option if password login is still "
        "allowed on the server:",
    )
    code(doc, f"ssh-copy-id -i ~/.ssh/{SSH_KEY}.pub root@{SERVER_IP}")
    body(doc, "Windows usually has no ssh-copy-id. Use method 2.")
    body(doc, "Method 2 — manual. Run it in PowerShell on the laptop:", bold=True)
    code(
        doc,
        f"type $HOME\\.ssh\\{SSH_KEY}.pub | ssh root@{SERVER_IP} "
        f'"mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys '
        f'&& chmod 600 ~/.ssh/authorized_keys"',
    )
    body(
        doc,
        "Permissions 700 on ~/.ssh and 600 on authorized_keys are mandatory: sshd "
        "refuses a file that anyone else can read. This is the most common cause of "
        "'I copied the key but it still asks for a password'.",
        italic=True,
    )

    heading(doc, "5.3. The ~/.ssh/config File on the Laptop", 2)
    body(
        doc,
        "Create the file ~/.ssh/config (on Windows: "
        "C:\\Users\\name\\.ssh\\config) and paste the following into it. This is the "
        f"current content of the file; it lets you write ssh {SSH_HOST} instead of "
        f"the full command with the IP and the key path.",
    )
    code(doc, SSH_CONFIG)
    body(doc, "What each line means:")
    table(
        doc,
        ["Line", "Why"],
        [
            [
                f"Host {SSH_HOST} {SERVER_IP}",
                f"Two names for one server: you can write ssh {SSH_HOST} or ssh {SERVER_IP}.",
            ],
            ["HostName / User", "Address and default user."],
            [
                f"IdentityFile ~/.ssh/{SSH_KEY}",
                "Which key to use. This exact file, not any other.",
            ],
            [
                "IdentitiesOnly yes",
                t(
                    "Use only the specified key instead of trying everything in ",
                    "ssh-agent. Removes the Too many authentication failures error.",
                ),
            ],
            [
                "AddKeysToAgent yes",
                "Enter the passphrase once, then the agent remembers the key until a reboot.",
            ],
            [
                "ServerAliveInterval 30\nServerAliveCountMax 4",
                t(
                    "Do not drop an idle connection: the server is probed at least ",
                    "every 30 seconds, with a disconnect after four missed replies.",
                ),
            ],
            ["TCPKeepAlive yes", "Keep the TCP connection alive at the socket level."],
            [
                t(
                    "PasswordAuthentication no\nKbdInteractiveAuthentication no\n",
                    "PreferredAuthentications publickey",
                ),
                t(
                    "Explicitly require a key. Even if password auth is accidentally ",
                    "re-enabled on the server, this laptop will not use it.",
                ),
            ],
        ],
    )

    heading(doc, "5.4. Verification", 2)
    code(doc, f'ssh -T {SSH_HOST}\nssh {SSH_HOST} "hostname && whoami"')
    body(
        doc,
        "The first command should connect without asking for a password. The second "
        "returns the server hostname and root.",
    )
    body(doc, "If it asks for a password, it is almost always one of three causes:")
    table(
        doc,
        ["Symptom", "Cause"],
        [
            [
                "Asks for a password",
                "The public part did not make it into ~/.ssh/authorized_keys. Check: "
                f'ssh {SSH_HOST} "grep -c . ~/.ssh/authorized_keys"',
            ],
            [
                "Permission denied (publickey)",
                "File permissions. ~/.ssh must be 700 and authorized_keys 600.",
            ],
            [
                "Rejected immediately, with no prompt",
                "Wrong key. Check that IdentityFile points to "
                f"~/.ssh/{SSH_KEY} and that this file exists.",
            ],
        ],
    )
    warn(
        doc,
        "The private key (~/.ssh/"
        + SSH_KEY
        + ") is never copied to the server and never handed to anyone. Only the "
        ".pub file goes to the server. If the private key falls into the wrong "
        "hands, delete its line from authorized_keys on the server immediately and "
        "create a new key.",
    )


def section_dsh(doc: Document) -> None:
    heading(doc, "6. Connecting to DeepSeek Harness", 1)
    body(
        doc,
        f"DeepSeek Harness is the AI agent's web interface. It runs on {SRV} as the "
        f"system service {DSH_UNIT} and listens only on 127.0.0.1:{DSH_PORT}. It is "
        f"not reachable from the internet — and rightly so, which is why access goes "
        f"through an SSH tunnel.",
    )

    heading(doc, "6.1. What That Means in Practice", 2)
    table(
        doc,
        ["Fact", "Consequence"],
        [
            [
                f"Listens on 127.0.0.1:{DSH_PORT}",
                "You cannot open it directly at the server address — only through a tunnel.",
            ],
            [
                f"Service {DSH_UNIT}, Restart=always",
                "Reboot the server and it comes back by itself. No manual start needed.",
            ],
            [
                "Requires a token in the address",
                "Opening the port is not enough: you need the address with the token parameter.",
            ],
            [
                "The token changes on every restart",
                "An old link stops working. Take the current one from the journal.",
            ],
        ],
    )

    heading(doc, "6.2. Connecting from the Laptop (Windows or Linux)", 2)
    body(doc, "Step 1. Open the SSH tunnel. In a separate terminal window:", bold=True)
    code(doc, f"ssh -N -L {DSH_PORT}:127.0.0.1:{DSH_PORT} {SSH_HOST}")
    body(
        doc,
        "The window stays occupied — that is normal; the tunnel works while it is "
        "open. The -N flag means 'run no command on the server, only forward the "
        "port'.",
        italic=True,
    )
    body(doc, "Step 2. In a second terminal window, get the current address:", bold=True)
    code(
        doc,
        f"ssh {SSH_HOST} \"journalctl -u {DSH_UNIT} -n 40 --no-pager | grep 'dsh web:' | tail -1\"",
    )
    body(
        doc,
        f"The command returns a line like dsh web: http://127.0.0.1:{DSH_PORT}/?token=... "
        f"— that is the address you need.",
    )
    body(doc, "Step 3. Open that address in your laptop browser.", bold=True)
    body(
        doc,
        f"Port {DSH_PORT} on the laptop now leads to the server, so "
        f"127.0.0.1:{DSH_PORT} in the browser is DeepSeek Harness.",
    )
    warn(
        doc,
        "Do not forward a link containing the token through messengers, and do not "
        "keep it in notes. The token grants full access to the agent. It changes "
        "whenever the service restarts, so after systemctl restart "
        f"{DSH_UNIT} take a fresh one.",
    )

    heading(doc, "6.3. Alternative: Forwarding Through VS Code", 2)
    body(
        doc,
        "If VS Code is already connected to the server, a separate tunnel is "
        f"unnecessary: on the PORTS tab click 'Forward a Port' and enter {DSH_PORT}. "
        f"Then open http://127.0.0.1:{DSH_PORT}/ — you will still need the token.",
    )

    heading(doc, "6.4. Managing the Service", 2)
    table(
        doc,
        ["Task", "Command"],
        [
            ["Status", f"systemctl status {DSH_UNIT}"],
            ["Restart", f"systemctl restart {DSH_UNIT}"],
            ["Stop", f"systemctl stop {DSH_UNIT}"],
            ["Logs", f"journalctl -u {DSH_UNIT} -f"],
            [
                "Address with token",
                f"journalctl -u {DSH_UNIT} -n 40 | grep 'dsh web:' | tail -1",
            ],
            ["Version", "dsh --version"],
        ],
    )
    body(
        doc,
        f"Stopping {DSH_UNIT} does not affect the AlphaAgent containers: they are "
        f"separate things. You can stop the agent and keep working on the project.",
        italic=True,
    )


def section_devcontainers(doc: Document) -> None:
    heading(doc, "7. Dev Containers: Detailed Instructions", 1)
    body(
        doc,
        "Scenario: several developers each connect from their own Windows laptop "
        f"to {SERVER} and work through the Dev Containers extension in VS Code.",
    )

    heading(doc, "7.1. How It Works", 2)
    body(
        doc,
        "One thing is essential to understand: the Dev Containers extension "
        "starts the container wherever Docker runs. Docker runs on "
        f"{SRV}. That means the code, the database and the computation live on the "
        f"server, while the laptop is used only as a screen and keyboard.",
    )
    table(
        doc,
        ["What lives where", "Where physically"],
        [
            ["Project files", f"On {SRV}, in /root/projects/AlphaAgent"],
            ["Containers, database, Redis", f"On {SRV}"],
            ["Terminal inside VS Code", "Inside the container on the server"],
            ["VS Code window", "On your laptop"],
            ["Browser with the control panel", "On the laptop, through a forwarded port"],
        ],
    )
    body(
        doc,
        "The main consequence: the laptop does not have to be powerful, and nothing "
        "needs to be installed on it except VS Code and an SSH client.",
    )

    heading(doc, "7.2. First Connection from a New Laptop", 2)
    numbered(doc, "Install VS Code and the Remote - SSH and Dev Containers extensions.")
    numbered(doc, "Create an SSH key and put the public part on the server — section 5.")
    numbered(
        doc,
        f"Connect to the server: Ctrl+Shift+P, then 'Remote-SSH: Connect to "
        f"Host', then {SSH_HOST}.",
    )
    numbered(doc, "Open the folder: File, then Open Folder, then /root/projects/AlphaAgent.")
    numbered(
        doc,
        "VS Code will offer 'Reopen in Container' — accept. Or "
        "Ctrl+Shift+P, then 'Dev Containers: Reopen in Container'.",
    )
    numbered(doc, "Wait for the build. The first time it takes a few minutes.")
    numbered(
        doc,
        "Run 'AlphaAgent: Migrate + seed demo data' once, then "
        "'AlphaAgent: Run server (in this container)'.",
    )
    numbered(doc, "Open the PORTS tab, find port 8000, click the globe icon.")
    body(
        doc,
        "After that, http://127.0.0.1:8000/ works in the laptop browser.",
        bold=True,
    )

    heading(doc, "7.3. What Happens When the Container Opens", 2)
    bullet(
        doc,
        "VS Code builds the same docker-compose.yml as a normal start, plus a "
        "thin overlay from .devcontainer. It is not a separate environment, so "
        "there can be no 'works in the container, fails in production' divergence;",
    )
    bullet(
        doc,
        "the web container starts with the sleep infinity command so VS Code has "
        "something to attach to. That is why the dev server must be started by hand "
        "(section 4);",
    )
    bullet(
        doc,
        "the project folder is mounted into the container, so edits in the editor "
        "are visible in the container immediately;",
    )
    bullet(
        doc,
        "closing the VS Code window stops the containers (the "
        "shutdownAction: stopCompose setting). This guards against walking away "
        "while the stack keeps holding ports.",
    )

    heading(doc, "7.4. Two Developers on One Server", 2)
    body(
        doc,
        "There is a fork in the road here, and it should be chosen deliberately. "
        "Both options work, but they behave differently.",
    )
    body(doc, "Option A. One shared set of containers.", bold=True)
    body(
        doc,
        "Both connect to the same web container. Simple, nothing to configure. "
        "This works when you work at different times or debug one task together.",
    )
    body(doc, "The downsides you need to know in advance:")
    bullet(
        doc,
        "one database for two: both see migrations and demo data, and "
        "'it broke on my side' can mean 'my partner just dropped a table';",
    )
    bullet(
        doc,
        "one runserver: if you started it, the second developer gets "
        "Address already in use. Exactly one person starts it;",
    )
    bullet(
        doc,
        "shared files: edits are visible immediately, and conflicts can happen "
        "right in the working folder.",
    )
    body(doc, "Option B. Each developer has their own set of containers.", bold=True)
    body(
        doc,
        "Full isolation: your own containers, your own database, your own ports. "
        "That lets you work at the same time without getting in each other's way. "
        f"It requires a few variables in the .env file on {SRV}.",
    )
    warn(
        doc,
        "Each developer must have their own .env, because the variables below "
        "differ. A shared .env means shared ports and shared conflicts.",
    )
    body(doc, "The first developer uses the defaults; nothing needs changing.")
    body(doc, f"The second developer adds the following to their .env on {SRV}:")
    code(
        doc,
        """COMPOSE_PROJECT_NAME=alphaagent-dev2
ALPHA_CONTAINER_PREFIX=alpha2
ALPHA_WEB_PORT=8081
ALPHA_DB_PORT=5433
ALPHA_REDIS_PORT=6380""",
    )
    table(
        doc,
        ["Variable", "What it isolates"],
        [
            ["COMPOSE_PROJECT_NAME", "A separate set of containers and a separate network."],
            ["ALPHA_CONTAINER_PREFIX", "Container names: alpha2_web instead of alpha_web."],
            ["ALPHA_WEB_PORT", "Control panel port on the server."],
            ["ALPHA_DB_PORT", "PostgreSQL port on the server."],
            ["ALPHA_REDIS_PORT", "Redis port on the server."],
        ],
    )
    body(
        doc,
        "After that the second developer works the same way, but their application "
        "opens at http://127.0.0.1:8081/, and the database is available on port 5433.",
    )
    warn(
        doc,
        "All these ports stay bound to 127.0.0.1. Never change the binding to "
        "0.0.0.0, regardless of how many developers there are — see section 11.",
    )

    heading(doc, "7.5. Common Failures", 2)
    table(
        doc,
        ["Symptom", "Cause and what to do"],
        [
            [
                "Site unreachable on port 8080",
                "Wrong port. In Dev Containers the application is on 8000 (section 3).",
            ],
            [
                "Site unreachable on port 8000",
                "The server is not running. The container starts with sleep infinity (section 4).",
            ],
            [
                "Log says Starting development server but the browser does not open",
                "Started on 127.0.0.1 instead of 0.0.0.0 (section 4).",
            ],
            [
                "Address already in use",
                "The server is already running — your second instance or a colleague's. See 7.4.",
            ],
            [
                "container name already in use",
                t(
                    "Two sets of containers share the same prefix. Set ",
                    "ALPHA_CONTAINER_PREFIX and COMPOSE_PROJECT_NAME (7.4).",
                ),
            ],
            [
                "Edits not visible in the container",
                t(
                    "The mount is broken. Check that ",
                    ".devcontainer/docker-compose.extend.yml mounts a single dot, not two dots.",
                ),
            ],
            [
                "env file .env not found",
                f"No .env file on {SRV}. Run make env.",
            ],
        ],
    )


def section_sync(doc: Document) -> None:
    heading(doc, "8. Keeping Work Environments in Sync", 1)
    body(
        doc,
        "When several people work on the project from different laptops, "
        "out-of-sync environments are the main source of mysterious failures. The "
        "rule is simple: whatever can live in Git lives in Git; whatever cannot is "
        "synced separately and deliberately.",
    )

    heading(doc, "8.1. What Is Shared and What Is Personal", 2)
    table(
        doc,
        ["What", "Where it lives", "How it is synced"],
        [
            ["Code, migrations, tests", "Git", "git pull and git push"],
            [
                ".env",
                f"Only on {SRV}, never in Git",
                "By hand, over a secure channel. See 8.2",
            ],
            [
                "docker-compose.yml, .devcontainer",
                "Git",
                "git pull — identical for everyone automatically",
            ],
            [
                "VS Code extensions",
                ".vscode/extensions.json in Git",
                "VS Code offers to install them when you open the folder",
            ],
            [
                "Personal VS Code settings",
                ".vscode/settings.json — not in Git",
                "Not synced, and that is fine",
            ],
            [
                "Database and Redis",
                "Docker volumes on the server",
                "Shared in option A, separate in option B",
            ],
        ],
    )

    heading(doc, "8.2. Syncing .env", 2)
    body(
        doc,
        "The .env file holds the database, Redis and Django passwords and the API "
        "key. It does not go into Git and must not. At the same time every "
        "participant must have the same values, otherwise one of you sees the "
        "database and the other does not.",
    )
    body(doc, "How to hand .env to a new developer properly:", bold=True)
    numbered(doc, f"Copy the file from {SRV} to a temporary location.")
    numbered(doc, "Send it over a secure channel: scp over SSH or a password manager.")
    numbered(doc, "Delete the temporary copy after sending.")
    code(
        doc,
        f"""# from the developer's laptop
scp {SSH_HOST}:/root/projects/AlphaAgent/.env ./env.backup
# send it to the colleague over a secure channel (not email, not a messenger)
# afterwards delete the temporary copies
rm ./env.backup""",
    )
    warn(
        doc,
        "Never send .env by email, in a messenger or in a tracker ticket. It is a "
        "file full of passwords. If it went there, consider the passwords "
        "compromised and change them.",
    )
    body(doc, "To verify that .env is not in Git (on the server or on the laptop):")
    code(
        doc,
        """git check-ignore -v .env                # should print a line from .gitignore
git log --all --full-history -- .env    # should be empty""",
    )
    body(
        doc,
        "If the second command returns something, the file was committed at some "
        "point. Changing the passwords is then not enough: the value is still in the "
        "history. See section 11.5.",
    )

    heading(doc, "8.3. Syncing VS Code Extensions and Settings", 2)
    body(
        doc,
        "The .vscode/extensions.json file lives in Git. When you open the folder, "
        "VS Code offers to install the listed extensions — that is the sync, and "
        "nothing needs to be done.",
    )
    body(
        doc,
        "The .vscode/settings.json file is deliberately not in Git: everyone has "
        "their own font, theme and indent size. It does not affect how the project "
        "works.",
        italic=True,
    )
    body(
        doc,
        "Settings inside the container are defined in .devcontainer/devcontainer.json "
        "and applied identically to everyone when the container is built.",
    )

    heading(doc, "8.4. Git Hygiene When Working as a Pair", 2)
    bullet(doc, "Before you start: git pull. Before you leave: git push.")
    bullet(
        doc,
        "Do not work in the same branch on the same server unless you have to. Different "
        "branches mean different tasks and fewer conflicts.",
    )
    bullet(
        doc,
        "Never run git add -A blindly. That command drags everything into the "
        "commit, including .env if it is accidentally not in .gitignore. Look at "
        "git status before committing.",
    )
    bullet(
        doc,
        "State files such as celerybeat-schedule change on their own. They are in "
        ".gitignore, and rightly so: do not commit them and do not try to fix them.",
    )


def section_laptops(doc: Document) -> None:
    heading(doc, "9. One Developer, Several Laptops", 1)
    body(
        doc,
        "The situation: you work from one laptop, then from another, and both "
        f"connect to {SERVER}. The good news: almost nothing needs to be done "
        f"differently, because the code and the environment live on the server, not "
        f"on the laptop.",
    )
    body(doc, "What every new laptop needs:", bold=True)
    table(
        doc,
        ["Step", "Why"],
        [
            ["VS Code, Remote-SSH, Dev Containers", "No connection without them"],
            [
                "Its own SSH key for this laptop",
                "Section 5. Do not copy a key from another laptop",
            ],
            ["A line in ~/.ssh/config", "Section 5.3"],
            ["Nothing else", "Git, Python and Docker are not needed on the laptop"],
        ],
    )
    warn(
        doc,
        "Create a separate key for each laptop rather than one shared key. If a "
        "laptop is lost, you delete one line from authorized_keys, and only that "
        "laptop loses access, not all your machines.",
    )
    body(doc, "How to revoke access from a lost laptop:", bold=True)
    code(
        doc,
        """# on the server: list the keys that exist
cat ~/.ssh/authorized_keys
# delete the line of the lost laptop (careful, do not delete the others)
nano ~/.ssh/authorized_keys""",
    )
    body(
        doc,
        "To tell keys apart, add a comment when creating them: "
        'ssh-keygen -C "laptop-name-date". It is visible at the end of the line in '
        "authorized_keys.",
        italic=True,
    )
    body(doc, "What not to do:", bold=True)
    bullet(
        doc,
        "copy the project folder to the laptop and work locally — that creates two "
        "diverging copies;",
    )
    bullet(doc, "copy the private SSH key between laptops;")
    bullet(
        doc,
        "set up Docker and a database of your own on the laptop — they will know "
        "nothing about the data on the server.",
    )
    body(
        doc,
        "The exception is working offline on purpose. See section 10: it explains "
        "how to get a copy, but that is a separate mode, not the normal one.",
        italic=True,
    )


def section_clone(doc: Document) -> None:
    heading(doc, "10. Cloning the Repository to a Laptop", 1)
    body(
        doc,
        "Under normal conditions there is no need to program on the laptop: the "
        "code and the environment live on the server. But a local copy can be "
        "useful — to read the code without a connection, make a quick documentation "
        "edit, or simply have a backup.",
    )
    warn(
        doc,
        "Cloning gives you files that are, in effect, read-only. If you start "
        "editing them on the laptop and committing while doing the same on the "
        "server in parallel, you will get diverging histories and conflicts. The "
        "server is the workplace.",
    )

    heading(doc, "10.1. Cloning", 2)
    body(
        doc,
        "Clone from GitHub, not from the server. GitHub is the source of truth; "
        "the server is just the machine where the project runs.",
    )
    code(
        doc,
        """# on the laptop
git clone https://github.com/SergeyGer/AlphaAgent.git
cd AlphaAgent""",
    )
    body(
        doc,
        "If the repository is private, you will need GitHub access: either a "
        "browser login (VS Code and Git Credential Manager can do that) or a "
        "personal access token. The token belongs in the credential manager, not in "
        "a file inside the project.",
    )

    heading(doc, "10.2. What the Repository Does Not Contain", 2)
    body(
        doc,
        "Some files are deliberately not stored in Git. They will be missing after "
        "cloning, and that is expected, not breakage.",
    )
    table(
        doc,
        ["File", "Why it is not in Git", "Where to get it"],
        [
            [
                ".env",
                "Contains passwords and API keys",
                f"From {SRV}: scp {SSH_HOST}:/root/projects/AlphaAgent/.env .",
            ],
            [
                "Run_Stop_Project.docx",
                "A personal reference, not part of the project",
                "This document. See 10.3",
            ],
            [
                ".vscode/settings.json",
                "Personal editor settings",
                "Not needed; VS Code creates its own",
            ],
            [
                "celerybeat-schedule*",
                "Scheduler state, changes by itself",
                "Appears on startup",
            ],
            [
                "node_modules, venv, htmlcov",
                "Build artefacts",
                "Created by the install commands",
            ],
        ],
    )

    heading(doc, "10.3. How to Keep .env and Run_Stop_Project.docx on Laptops", 2)
    body(
        doc,
        "Both files are needed on every laptop, but neither may go into Git. A "
        "practical approach is a separate location synced outside the project's Git.",
    )
    body(doc, "Option 1. A private Git repository for personal files.", bold=True)
    code(
        doc,
        """alphaagent-private/
    .env
    Run_Stop_Project.docx
    README.md          # what this is and why it lives here""",
    )
    body(
        doc,
        "A separate private repository for exactly these files. Pro: versioning "
        "and access from any laptop. Con: .env now lives in Git, even if the "
        "repository is private. Acceptable only if you understand the risk and have "
        "enabled two-factor authentication on GitHub.",
    )
    body(doc, "Option 2. A password manager.", bold=True)
    body(
        doc,
        "1Password, Bitwarden, KeePass store .env as a secure note and the document "
        "as an attachment. Pro: not in Git at all. This is the preferred option "
        "for .env.",
    )
    body(doc, "Option 3. A copy on the server and scp on demand.", bold=True)
    body(
        doc,
        f"The source of truth for .env is {SRV}. A new laptop fetches the file "
        f"with one command when it actually needs it:",
    )
    code(doc, f"scp {SSH_HOST}:/root/projects/AlphaAgent/.env ./")
    body(
        doc,
        "This option works for Run_Stop_Project.docx too: the document sits on the "
        "server in the project root and never enters Git, so it can be fetched the "
        "same way.",
    )
    warn(
        doc,
        f"Whichever option you choose, .env must have a backup outside {SRV}. If "
        f"the server is lost and .env existed only there, the passwords will have to "
        f"be restored from memory.",
    )


def section_security(doc: Document) -> None:
    heading(doc, "11. Security", 1)
    body(
        doc,
        f"The server {SERVER_IP} has a public IP address, so internet-wide scanners "
        f"see it around the clock. Below is what is already done and what is worth "
        f"doing.",
    )

    heading(doc, "11.1. What Is Already Locked Down", 2)
    table(
        doc,
        ["What", "State"],
        [
            ["PostgreSQL 5432", "127.0.0.1 only — unreachable from the internet"],
            ["Redis 6379", "127.0.0.1 only, and password-protected"],
            ["SSH password login", "Disabled, keys only"],
            ["DeepSeek Harness 3080", "127.0.0.1 only, access through a tunnel"],
            [
                "Control panel 8080",
                "Reachable from the server; Django rejects foreign Host headers",
            ],
        ],
    )
    body(
        doc,
        "This is verified and enforced by an automated test: if someone publishes a "
        "database to the internet again, the build fails.",
        italic=True,
    )

    heading(doc, "11.2. fail2ban — Brute-Force Protection", 2)
    body(
        doc,
        "Even with SSH password auth disabled, someone will keep knocking. fail2ban "
        "reads the logs and temporarily blocks the addresses that produce failed "
        "attempts.",
    )
    body(doc, f"Install (on {SRV}, as root):", bold=True)
    code(
        doc,
        """apt update && apt install -y fail2ban
systemctl enable --now fail2ban""",
    )
    body(doc, "Configuration in /etc/fail2ban/jail.local:", bold=True)
    code(
        doc,
        """[DEFAULT]
# Block for 1 hour after 5 failures within 10 minutes
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = %(sshd_backend)s""",
    )
    body(doc, "Apply and verify:", bold=True)
    code(
        doc,
        """systemctl restart fail2ban
fail2ban-client status sshd""",
    )
    body(
        doc,
        "The output shows the number of banned addresses. If it grows, the "
        "protection is working and cutting off automated attempts.",
    )
    warn(
        doc,
        "Do not set bantime too high and do not ban forever: if you mistype a "
        "password from a new laptop, you will lock yourself out. Start with "
        "bantime 1h and maxretry 5.",
    )
    body(doc, "To unban your own address if you do get blocked:")
    code(doc, "fail2ban-client set sshd unbanip YOUR.IP.ADDRESS")

    heading(doc, "11.3. Firewall (ufw)", 2)
    body(
        doc,
        f"ufw is currently not active on {SRV}. Port 8080 is open to the outside, "
        f"and that is a deliberate decision — but it is better to limit what is "
        f"visible at all.",
    )
    body(doc, "The minimal setup that will not cut you off from the server:", bold=True)
    code(
        doc,
        """# FIRST allow SSH, otherwise you lose access
ufw allow 22/tcp
# Then enable
ufw --force enable
ufw status verbose""",
    )
    warn(
        doc,
        "The order is mandatory: ufw allow 22/tcp first, enable only after that. "
        "If you enable it earlier, the SSH connection drops and you will have no way "
        "in — you will need the hosting provider's console.",
    )
    body(
        doc,
        "Whether you need 8080 on the outside is your call. If you work only "
        "through VS Code, you do not need it: port forwarding runs over SSH. Then 8080 can "
        "be closed as well:",
    )
    code(
        doc,
        """ufw deny 8080/tcp
# and additionally bind the panel to localhost:
# in .env   ALPHA_WEB_PORT=127.0.0.1:8080""",
    )

    heading(doc, "11.4. Docker and Permissions", 2)
    bullet(
        doc,
        "Do not add users to the docker group unless it is necessary: membership is "
        "equivalent to root, because it lets you mount any host directory into a "
        "container.",
    )
    bullet(
        doc,
        "Keep databases on 127.0.0.1. That is already done and covered by a test.",
    )
    bullet(
        doc,
        "Never publish port 2375 (the Docker API) under any circumstances.",
    )
    bullet(
        doc,
        "Update images: docker compose pull and a rebuild once a month. An outdated "
        "image is the most common cause of a breach.",
    )

    heading(doc, "11.5. Secrets", 2)
    table(
        doc,
        ["Rule", "Why"],
        [
            [
                ".env never goes into Git",
                "It holds the database, Redis and Django passwords and the API key",
            ],
            [
                "A secret that reached Git history counts as leaked",
                "It stays in old commits even after the file is deleted",
            ],
            [
                "The DeepSeek Harness token is a password",
                "It grants full access to the agent",
            ],
            [
                "The Anthropic API key must not go into code",
                "Only into .env, and only on the server",
            ],
            [
                ".env permissions are 600",
                "So that only the owner can read it: chmod 600 .env",
            ],
        ],
    )
    body(doc, "If a secret did reach Git, the order of action is:")
    numbered(doc, "Change the secret itself immediately — that is the main step.")
    numbered(doc, "Remove it from history: git filter-repo or BFG Repo-Cleaner.")
    numbered(doc, "Check whether anyone else has used it (the provider's logs).")
    numbered(
        doc,
        "Only then treat the incident as closed: removing it from history without changing the secret is pointless.",
    )
    body(doc, "Check that nothing extra is currently tracked:")
    code(
        doc,
        """git ls-files | grep -E "\\.env$|token|secret|key" || echo "clean\"""",
    )


def section_commands(doc: Document) -> None:
    heading(doc, "12. Useful Commands", 1)
    table(
        doc,
        ["Task", "Command"],
        [
            ["Start everything", "make start"],
            ["Stop, leaving it built", "make stop"],
            ["Stop and free the ports", "make down"],
            ["See what is running", "make status"],
            ["Logs of all services", "make logs"],
            ["Django console", "make shell"],
            ["Tests", "make test"],
            ["Linter", "make lint"],
            ["Everything at once: lint, checks, tests", "make check"],
            ["Log in to the server", f"ssh {SSH_HOST}"],
            [
                "Tunnel to DeepSeek Harness",
                f"ssh -N -L {DSH_PORT}:127.0.0.1:{DSH_PORT} {SSH_HOST}",
            ],
            [
                "DeepSeek Harness address with token",
                f"journalctl -u {DSH_UNIT} -n 40 | grep 'dsh web:' | tail -1",
            ],
            ["DeepSeek Harness status", f"systemctl status {DSH_UNIT}"],
            ["fail2ban status", "fail2ban-client status sshd"],
            ["Firewall status", "ufw status verbose"],
        ],
    )


def section_troubleshooting(doc: Document) -> None:
    heading(doc, "13. When Something Does Not Work", 1)
    table(
        doc,
        ["Message or symptom", "What to do"],
        [
            [
                "port is already allocated",
                t(
                    "The port is taken by another project. Run make down here or in the other ",
                    "project, or change the port in .env (ALPHA_WEB_PORT and the others).",
                ),
            ],
            [
                "env file .env not found",
                f"No .env on {SRV}. Run make env.",
            ],
            [
                "Cannot connect to the Docker daemon",
                t(
                    "Docker is not running: systemctl start docker on the server, or ",
                    "start Docker Desktop on Windows.",
                ),
            ],
            [
                "Page does not open right after start",
                "Wait 30-40 seconds: the database has to become ready. make status.",
            ],
            [
                "Permission denied (publickey)",
                "Permissions 700/600 on ~/.ssh and authorized_keys, or the wrong key (section 5.4).",
            ],
            [
                "Address already in use inside the container",
                "The server is already running — your second instance or a colleague's (section 7.4).",
            ],
            [
                "DeepSeek Harness: authentication required",
                "The link has no token, or the token is stale. Get a fresh one from the journal (section 6.2).",
            ],
            [
                "DeepSeek Harness does not open at all",
                "The tunnel is not up. Check that the ssh -N -L window is open (section 6.2).",
            ],
        ],
    )


def section_cheatsheet(doc: Document) -> None:
    heading(doc, "14. Cheat Sheet", 1)
    body(doc, "If you remember only this, you can already work:", bold=True)
    code(
        doc,
        """# work
ssh Hetzner
cd ~/projects/AlphaAgent
make start

# browser (on the server)
http://127.0.0.1:8080/

# done
make down""",
    )
    body(doc, "Through Dev Containers:", bold=True)
    code(
        doc,
        """Ctrl+Shift+P -> Dev Containers: Reopen in Container
Ctrl+Shift+P -> Tasks: Run Task -> AlphaAgent: Run server (in this container)
# browser: http://127.0.0.1:8000/
# port in the browser is shown on the PORTS tab""",
    )
    body(doc, "DeepSeek Harness:", bold=True)
    code(
        doc,
        f"""ssh -N -L {DSH_PORT}:127.0.0.1:{DSH_PORT} Hetzner        # window 1, leave it open
ssh Hetzner "journalctl -u {DSH_UNIT} -n 40 | grep 'dsh web:' | tail -1"   # window 2
# open the resulting address in the browser""",
    )
    doc.add_paragraph()
    body(
        doc,
        "This file was generated by scripts/make_run_stop_doc.py. If the commands "
        "in the project change, run the script again.",
        italic=True,
    )


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------


def verify_no_phantom_keys(text: str) -> list[str]:
    """SSH key filenames the document names that the configured key is not.

    A guide that says "load your key" without saying which file is merely vague.
    One that names a file absent from the reader's machine is actively harmful:
    they run the command, get "No such file", and conclude the documented setup is
    broken rather than that the document is stale.
    """
    phantom: list[str] = []
    for alias in SSH_KEY_ALIASES:
        if re.search(rf"(?<![\w.-]){re.escape(alias)}(?![\w.-])", text):
            phantom.append(alias)
    return phantom


def build() -> Path:
    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    intro(doc)
    section_quick(doc)
    section_run_stop(doc)
    section_ports(doc)
    section_not_serving(doc)
    section_ssh(doc)
    section_dsh(doc)
    section_devcontainers(doc)
    section_sync(doc)
    section_laptops(doc)
    section_clone(doc)
    section_security(doc)
    section_commands(doc)
    section_troubleshooting(doc)
    section_cheatsheet(doc)

    # Collect what the reader will actually see, not what the code intended.
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            parts.extend(cell.text for cell in row.cells)
    text = "\n".join(parts)

    phantom = verify_no_phantom_keys(text)
    if phantom:
        sys.exit(
            "refusing to write the document: it names SSH key file(s) the configured "
            f"IdentityFile does not use: {', '.join(phantom)}. "
            f"Only ~/.ssh/{SSH_KEY} exists for this reader."
        )
    if SSH_KEY not in text:
        sys.exit(f"refusing to write: the document never mentions ~/.ssh/{SSH_KEY}")
    if SERVER_IP not in text:
        sys.exit(f"refusing to write: the document never mentions {SERVER_IP}")

    doc.save(OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    path = build()
    print(f"written: {path}  ({path.stat().st_size} bytes)")
    print(f"self-check: only ~/.ssh/{SSH_KEY} referenced; server IP present")
