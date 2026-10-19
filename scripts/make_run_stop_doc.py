#!/usr/bin/env python
# ruff: noqa: RUF001, RUF003
#
# RUF001-3 flag Cyrillic characters that look like Latin ones. This file is a
# Russian-language document generator, so those characters are the entire point;
# the rule is silenced HERE rather than in pyproject.toml so it keeps guarding
# every other file in the project, where a stray Cyrillic "с" in an identifier or
# a URL would be a genuine bug that is almost impossible to see.
"""Generate Run_Stop_Project.docx - the Russian-language operator guide.

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
SERVER = f"Linux-сервер (текущий реальный Linux сервер {SERVER_IP})"
SRV = f"сервер {SERVER_IP}"

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
    title = doc.add_heading("AlphaAgent — рабочий справочник оператора", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    body(
        doc,
        "Всё, что нужно, чтобы начать работу над проектом, остановить его, "
        "подключиться с нового ноутбука и не оставить открытым то, что должно "
        "быть закрыто.",
        italic=True,
    )
    table(
        doc,
        ["Раздел", "Когда нужен"],
        [
            ["1–4", "Повседневная работа: запустить, остановить, открыть в браузере"],
            ["5", "Первый вход с нового ноутбука: SSH-ключ и настройка"],
            ["6", "Подключение к DeepSeek Harness"],
            ["7–8", "Dev Containers, в том числе вдвоём на одном сервере"],
            ["9–10", "Несколько ноутбуков, клонирование репозитория"],
            ["11", "Безопасность: fail2ban, ufw, секреты"],
            ["12–14", "Справочник команд, разбор ошибок, шпаргалка"],
        ],
    )
    body(
        doc,
        f"Команды в документе выполняются либо в терминале {SERVER}, либо в "
        f"терминале VS Code, подключённом к нему. Работа на ноутбуке без "
        f"подключения к серверу оговаривается отдельно.",
        italic=True,
    )


def section_quick(doc: Document) -> None:
    heading(doc, "1. Главное в двух словах", 1)
    body(
        doc,
        f"Проект состоит из шести контейнеров и работает на {SERVER}. "
        f"Запускаются и останавливаются они одной командой — вручную запускать "
        f"каждый не нужно.",
    )
    table(
        doc,
        ["Контейнер", "Что это", "Порт на сервере"],
        [
            ["web", "Веб-сервер: API, панель управления, WebSocket", "8080"],
            ["worker", "Celery: выполняет задачи ИИ-агента", "—"],
            ["beat", "Планировщик: запускает обходы по расписанию", "—"],
            ["mcp", "Сервер инструментов (котировки, новости)", "—"],
            ["db", "PostgreSQL 16 — база данных", "5432 (только localhost)"],
            ["redis", "Redis 7 — очередь задач и кэш", "6379 (только localhost)"],
        ],
    )
    body(
        doc,
        f"Панель управления после запуска: http://127.0.0.1:8080/ "
        f"(на {SRV}). Логин demo, пароль demo-pass-123.",
    )
    warn(
        doc,
        "Базы данных доступны только с самого сервера. Это сделано намеренно: "
        "раньше PostgreSQL и Redis были открыты в интернет. Подробности — "
        "в разделе 11.",
    )


def section_run_stop(doc: Document) -> None:
    heading(doc, "2. Запуск и остановка", 1)

    heading(doc, "2.1. Запуск", 2)
    body(doc, "Три способа. Выберите удобный — результат одинаковый.")
    body(doc, "Способ 1. Из VS Code (одинаково на Windows и на Linux):", bold=True)
    bullet(doc, "Ctrl+Shift+P")
    bullet(doc, "наберите Run Task и выберите «Tasks: Run Task»")
    bullet(doc, "выберите «AlphaAgent: Start»")
    body(doc, "Способ 2. В терминале (Linux, macOS, WSL, Git Bash):", bold=True)
    code(doc, "make start")
    body(doc, "Способ 3. Напрямую через Docker (работает везде):", bold=True)
    code(doc, "docker compose up -d --build")
    body(
        doc,
        "Первый запуск занимает несколько минут: Docker собирает образ. "
        "Последующие — несколько секунд.",
    )

    heading(doc, "2.2. Остановка — самый важный раздел", 2)
    body(
        doc,
        "Команд две, и они делают разное. От выбора зависит, будут ли сервисы "
        "разных проектов мешать друг другу.",
    )
    table(
        doc,
        ["Команда", "Что происходит", "Когда использовать"],
        [
            [
                "make stop\n(«AlphaAgent: Stop»)",
                "Контейнеры останавливаются, но остаются собранными. Порты и память освобождаются.",
                "Короткий перерыв. Запуск после этого — мгновенный.",
            ],
            [
                "make down\n(«AlphaAgent: Shut down»)",
                "Контейнеры удаляются, сеть освобождается полностью.",
                t(
                    "Переключение на другой проект надолго. Именно это освобождает ",
                    "порты 8080, 5432 и 6379.",
                ),
            ],
        ],
    )
    warn(
        doc,
        "Переключаетесь на другой проект — используйте down. Остановленные "
        "контейнеры в некоторых конфигурациях Docker Desktop продолжают удерживать "
        "порты, и тогда второй проект не сможет запуститься.",
    )
    body(
        doc,
        "Данные не теряются: ни stop, ни down не удаляют тома. Удаляет данные "
        "только down -v, и эта команда намеренно не обёрнута ни в одну задачу — "
        "чтобы её нельзя было выполнить одной опечаткой.",
        italic=True,
    )

    heading(doc, "2.3. Переключение между проектами", 2)
    code(
        doc,
        """# Закончили с AlphaAgent
cd ~/projects/AlphaAgent
make down

# ... работаете над другим проектом ...

# Вернулись
cd ~/projects/AlphaAgent
make start""",
    )

    heading(doc, "2.4. Автозапуск отключён", 2)
    body(
        doc,
        "Ни один контейнер не запускается сам — ни после перезагрузки сервера, "
        "ни после перезапуска Docker. Это сделано намеренно:",
    )
    bullet(doc, "проект не занимает память и порты, пока вы с ним не работаете;")
    bullet(doc, "автономная торговля не возобновляется без вашего ведома;")
    bullet(doc, "сервисы разных проектов не конкурируют за одни и те же порты.")
    body(doc, "Проверить — в ответе должно быть no для всех контейнеров:")
    code(doc, "docker inspect alpha_web --format '{{.HostConfig.RestartPolicy.Name}}'")
    body(
        doc,
        "Исключение — DeepSeek Harness: он запускается как системная служба и "
        "после перезагрузки поднимается сам. Это отдельный сервис, см. раздел 6.",
        italic=True,
    )


def section_ports(doc: Document) -> None:
    heading(doc, "3. Какой порт открывать", 1)
    body(
        doc,
        "К одному и тому же приложению ведут два пути, и номера портов у них "
        "разные. Это самое частое место ошибки: приложение работает, а браузер "
        "пишет, что сайт недоступен.",
    )
    table(
        doc,
        ["Где вы находитесь", "Как идёт трафик", "Адрес в браузере"],
        [
            [
                "В VS Code, внутри Dev Container",
                "VS Code прокидывает порт 8000 контейнера на ваш ноутбук",
                "http://127.0.0.1:8000/",
            ],
            [
                f"На {SRV} (по SSH)",
                "Docker публикует порт 8000 контейнера как 8080 на сервере",
                "http://127.0.0.1:8080/",
            ],
        ],
    )
    body(
        doc,
        "Это одно и то же приложение. Номера разные потому, что это два разных "
        "механизма, а не два разных сервера.",
    )
    warn(
        doc,
        "Работаете через Dev Containers — открывайте порт 8000. Порт 8080 "
        "существует только на стороне сервера; внутри контейнера на нём ничего не "
        "слушает, поэтому прокидывать его бессмысленно.",
    )
    body(
        doc,
        "Посмотреть, какие порты VS Code уже прокинул, можно на вкладке PORTS — "
        "рядом с вкладкой TERMINAL внизу окна VS Code.",
    )


def section_not_serving(doc: Document) -> None:
    heading(doc, "4. Приложение не открывается сразу после старта контейнера", 1)
    body(
        doc,
        "Так и задумано. В режиме Dev Container контейнер web держится живым "
        "специально, чтобы VS Code мог к нему подключиться, но сам веб-сервер не "
        "запускается. Пока вы его не запустите, открывать нечего.",
    )
    body(doc, "Способ 1 — через список задач VS Code:", bold=True)
    bullet(doc, "Ctrl+Shift+P, затем «Tasks: Run Task»")
    bullet(doc, "выберите «AlphaAgent: Run server (in this container)»")
    body(doc, "Способ 2 — в терминале внутри контейнера:", bold=True)
    code(doc, "python manage.py runserver 0.0.0.0:8000")
    warn(
        doc,
        "0.0.0.0 обязательно. По умолчанию Django слушает только 127.0.0.1 — "
        "собственный loopback контейнера. Проброшенный порт в этом случае "
        "подключается в пустоту, и браузер показывает ошибку, хотя в логе написано "
        "Starting development server.",
    )
    body(doc, "Если демо-данных ещё нет, один раз выполните:")
    code(
        doc,
        """python manage.py migrate
python manage.py seed_demo""",
    )
    body(
        doc,
        "Или выберите задачу «AlphaAgent: Migrate + seed demo data» — она делает и то, и другое.",
    )


def section_ssh(doc: Document) -> None:
    heading(doc, "5. Доступ к серверу: SSH-ключ с нуля", 1)
    body(
        doc,
        f"Все дальнейшие разделы предполагают, что вы уже можете войти на {SERVER} "
        f"без пароля. Если вы на новом ноутбуке — начните отсюда.",
    )
    body(
        doc,
        "Вход по паролю на сервере отключён. Это правильно и должно так "
        "остаться: пароль можно подобрать, ключ — практически нет.",
        italic=True,
    )

    heading(doc, "5.1. Создание ключа на ноутбуке (Windows)", 2)
    body(
        doc,
        "Откройте PowerShell. Команда ssh-keygen входит в Windows 10 и 11 "
        "начиная с версии 1809, отдельно ставить ничего не нужно.",
    )
    code(
        doc,
        f'ssh-keygen -t ed25519 -C "ваш-ноутбук" -f $HOME\\.ssh\\{SSH_KEY}',
    )
    body(doc, "На вопрос о парольной фразе (passphrase):")
    bullet(doc, "укажите её — тогда украденный файл ключа сам по себе бесполезен;")
    bullet(
        doc,
        "либо нажмите Enter дважды — удобнее, но файл ключа становится "
        "единственной защитой. Для личного ноутбука это приемлемо, для общего — нет.",
    )
    body(doc, "Создадутся два файла:")
    table(
        doc,
        ["Файл", "Что это", "Кому можно показывать"],
        [
            [f"~/.ssh/{SSH_KEY}", "ЗАКРЫТЫЙ ключ", "Никому. Никогда."],
            [f"~/.ssh/{SSH_KEY}.pub", "Открытый ключ", "Можно класть на сервер"],
        ],
    )
    body(doc, "На Linux и macOS команда та же, только путь без обратной косой черты:")
    code(doc, f'ssh-keygen -t ed25519 -C "ваш-ноутбук" -f ~/.ssh/{SSH_KEY}')

    heading(doc, "5.2. Кладём открытую часть на сервер", 2)
    body(
        doc,
        "Способ 1 — автоматический. Самый простой, если пароль на сервере ещё разрешён:",
    )
    code(doc, f"ssh-copy-id -i ~/.ssh/{SSH_KEY}.pub root@{SERVER_IP}")
    body(doc, "На Windows команды ssh-copy-id обычно нет. Используйте способ 2.")
    body(doc, "Способ 2 — вручную. Выполняется в PowerShell на ноутбуке:", bold=True)
    code(
        doc,
        f"type $HOME\\.ssh\\{SSH_KEY}.pub | ssh root@{SERVER_IP} "
        f'"mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys '
        f'&& chmod 600 ~/.ssh/authorized_keys"',
    )
    body(
        doc,
        "Права 700 на ~/.ssh и 600 на authorized_keys обязательны: sshd "
        "отказывается использовать файл, который может прочитать кто-то ещё. Это "
        "самая частая причина ситуации «ключ положил, а не пускает».",
        italic=True,
    )

    heading(doc, "5.3. Файл ~/.ssh/config на ноутбуке", 2)
    body(
        doc,
        "Создайте файл ~/.ssh/config (на Windows — "
        "C:\\Users\\имя\\.ssh\\config) и вставьте в него следующее. Это текущее "
        f"содержимое файла; оно позволяет писать ssh {SSH_HOST} вместо полной "
        f"команды с IP и путём к ключу.",
    )
    code(doc, SSH_CONFIG)
    body(doc, "Что означает каждая строка:")
    table(
        doc,
        ["Строка", "Зачем"],
        [
            [
                f"Host {SSH_HOST} {SERVER_IP}",
                f"Два имени для одного сервера: можно писать ssh {SSH_HOST} или ssh {SERVER_IP}.",
            ],
            ["HostName / User", "Адрес и пользователь по умолчанию."],
            [
                f"IdentityFile ~/.ssh/{SSH_KEY}",
                "Какой ключ использовать. Именно этот файл, не какой-либо другой.",
            ],
            [
                "IdentitiesOnly yes",
                t(
                    "Использовать только указанный ключ, а не перебирать всё из ",
                    "ssh-agent. Убирает ошибку Too many authentication failures.",
                ),
            ],
            [
                "AddKeysToAgent yes",
                "Один раз ввели парольную фразу — дальше агент помнит ключ до перезагрузки.",
            ],
            [
                "ServerAliveInterval 30\nServerAliveCountMax 4",
                t(
                    "Не рвать соединение при простое: сервер опрашивается не реже ",
                    "раза в 30 секунд, разрыв после четырёх пропусков.",
                ),
            ],
            ["TCPKeepAlive yes", "Держать TCP-соединение живым на уровне сокета."],
            [
                t(
                    "PasswordAuthentication no\nKbdInteractiveAuthentication no\n",
                    "PreferredAuthentications publickey",
                ),
                t(
                    "Явно требовать ключ. Даже если на сервере пароль случайно ",
                    "включат, этот ноутбук им не воспользуется.",
                ),
            ],
        ],
    )

    heading(doc, "5.4. Проверка", 2)
    code(doc, f'ssh -T {SSH_HOST}\nssh {SSH_HOST} "hostname && whoami"')
    body(
        doc,
        "Первая команда должна подключиться без запроса пароля. Вторая вернёт имя сервера и root.",
    )
    body(doc, "Если просит пароль — почти всегда одна из трёх причин:")
    table(
        doc,
        ["Симптом", "Причина"],
        [
            [
                "Просит пароль",
                "Открытая часть не попала в ~/.ssh/authorized_keys. Проверьте: "
                f'ssh {SSH_HOST} "grep -c . ~/.ssh/authorized_keys"',
            ],
            [
                "Permission denied (publickey)",
                "Права на файлы. Должно быть 700 у ~/.ssh и 600 у authorized_keys.",
            ],
            [
                "Сразу отказ, без запроса",
                "Ключ не тот. Проверьте, что IdentityFile указывает на "
                f"~/.ssh/{SSH_KEY} и что этот файл существует.",
            ],
        ],
    )
    warn(
        doc,
        "Закрытый ключ (~/.ssh/"
        + SSH_KEY
        + ") не копируется на сервер и не передаётся никому. На сервер кладётся "
        "только файл с расширением .pub. Если закрытый ключ попал в третьи руки — "
        "его строку немедленно удаляют из authorized_keys на сервере и создают "
        "новый ключ.",
    )


def section_dsh(doc: Document) -> None:
    heading(doc, "6. Подключение к DeepSeek Harness", 1)
    body(
        doc,
        f"DeepSeek Harness — веб-интерфейс ИИ-агента. Он работает на {SRV} как "
        f"системная служба {DSH_UNIT} и слушает только 127.0.0.1:{DSH_PORT}. Из "
        f"интернета он недоступен — и это правильно, поэтому подключение идёт "
        f"через SSH-туннель.",
    )

    heading(doc, "6.1. Что это значит на практике", 2)
    table(
        doc,
        ["Факт", "Следствие"],
        [
            [
                f"Слушает 127.0.0.1:{DSH_PORT}",
                "Напрямую по адресу сервера открыть нельзя — только через туннель.",
            ],
            [
                f"Служба {DSH_UNIT}, Restart=always",
                "Перезагрузили сервер — поднялась сама. Запускать вручную не нужно.",
            ],
            [
                "Требуется токен в адресе",
                "Просто открыть порт недостаточно: нужен адрес с параметром token.",
            ],
            [
                "Токен меняется при каждом перезапуске",
                "Старая ссылка перестаёт работать. Берём актуальную из журнала.",
            ],
        ],
    )

    heading(doc, "6.2. Подключение с ноутбука (Windows или Linux)", 2)
    body(doc, "Шаг 1. Откройте SSH-туннель. В отдельном окне терминала:", bold=True)
    code(doc, f"ssh -N -L {DSH_PORT}:127.0.0.1:{DSH_PORT} {SSH_HOST}")
    body(
        doc,
        "Окно останется занятым — это нормально, туннель работает, пока оно "
        "открыто. Флаг -N означает «не выполнять команду на сервере, только "
        "пробросить порт».",
        italic=True,
    )
    body(doc, "Шаг 2. Во втором окне терминала возьмите актуальный адрес:", bold=True)
    code(
        doc,
        f"ssh {SSH_HOST} \"journalctl -u {DSH_UNIT} -n 40 --no-pager | grep 'dsh web:' | tail -1\"",
    )
    body(
        doc,
        f"Команда вернёт строку вида dsh web: http://127.0.0.1:{DSH_PORT}/?token=… "
        f"— это и есть нужный адрес.",
    )
    body(doc, "Шаг 3. Откройте этот адрес в браузере ноутбука.", bold=True)
    body(
        doc,
        f"Порт {DSH_PORT} на ноутбуке теперь ведёт на сервер, поэтому адрес "
        f"127.0.0.1:{DSH_PORT} в браузере — это DeepSeek Harness.",
    )
    warn(
        doc,
        "Не пересылайте ссылку с токеном в мессенджерах и не храните её в "
        "заметках. Токен даёт полный доступ к агенту. Он меняется при перезапуске "
        f"службы, поэтому после systemctl restart {DSH_UNIT} возьмите новый.",
    )

    heading(doc, "6.3. Альтернатива: проброс через VS Code", 2)
    body(
        doc,
        "Если VS Code уже подключён к серверу, отдельный туннель не нужен: на "
        f"вкладке PORTS нажмите «Forward a Port» и укажите {DSH_PORT}. Дальше "
        f"откройте http://127.0.0.1:{DSH_PORT}/ — токен всё равно потребуется.",
    )

    heading(doc, "6.4. Управление службой", 2)
    table(
        doc,
        ["Задача", "Команда"],
        [
            ["Статус", f"systemctl status {DSH_UNIT}"],
            ["Перезапустить", f"systemctl restart {DSH_UNIT}"],
            ["Остановить", f"systemctl stop {DSH_UNIT}"],
            ["Логи", f"journalctl -u {DSH_UNIT} -f"],
            [
                "Адрес с токеном",
                f"journalctl -u {DSH_UNIT} -n 40 | grep 'dsh web:' | tail -1",
            ],
            ["Версия", "dsh --version"],
        ],
    )
    body(
        doc,
        f"Остановка {DSH_UNIT} не влияет на контейнеры AlphaAgent: это разные "
        f"вещи. Можно остановить агента и продолжить работать с проектом.",
        italic=True,
    )


def section_devcontainers(doc: Document) -> None:
    heading(doc, "7. Dev Containers: подробная инструкция", 1)
    body(
        doc,
        "Сценарий: несколько разработчиков подключаются каждый со своего "
        f"ноутбука с Windows к {SERVER} и работают через расширение Dev "
        f"Containers в VS Code.",
    )

    heading(doc, "7.1. Как это устроено", 2)
    body(
        doc,
        "Принципиально важно понять одну вещь: расширение Dev Containers "
        "запускает контейнер там, где работает Docker. Docker работает на "
        f"{SRV}. Значит, код, база данных и вычисления находятся на сервере, а "
        f"ноутбук используется только как экран и клавиатура.",
    )
    table(
        doc,
        ["Что где живёт", "Где физически"],
        [
            ["Файлы проекта", f"На {SRV}, в /root/projects/AlphaAgent"],
            ["Контейнеры, база, Redis", f"На {SRV}"],
            ["Терминал внутри VS Code", "Внутри контейнера на сервере"],
            ["Окно VS Code", "На вашем ноутбуке"],
            ["Браузер с панелью управления", "На ноутбуке, через проброс порта"],
        ],
    )
    body(
        doc,
        "Из этого следует главное: ноутбук не обязан быть мощным, и на нём не "
        "нужно ничего устанавливать, кроме VS Code и клиента SSH.",
    )

    heading(doc, "7.2. Первое подключение с нового ноутбука", 2)
    numbered(doc, "Установить VS Code и расширения Remote - SSH и Dev Containers.")
    numbered(doc, "Создать SSH-ключ и положить открытую часть на сервер — раздел 5.")
    numbered(
        doc,
        f"Подключиться к серверу: Ctrl+Shift+P, затем «Remote-SSH: Connect to "
        f"Host», затем {SSH_HOST}.",
    )
    numbered(doc, "Открыть папку: File, затем Open Folder, затем /root/projects/AlphaAgent.")
    numbered(
        doc,
        "VS Code предложит «Reopen in Container» — согласиться. Либо "
        "Ctrl+Shift+P, затем «Dev Containers: Reopen in Container».",
    )
    numbered(doc, "Дождаться сборки. В первый раз это несколько минут.")
    numbered(
        doc,
        "Один раз выполнить «AlphaAgent: Migrate + seed demo data», затем "
        "«AlphaAgent: Run server (in this container)».",
    )
    numbered(doc, "Открыть вкладку PORTS, найти порт 8000, нажать значок глобуса.")
    body(
        doc,
        "После этого в браузере ноутбука работает http://127.0.0.1:8000/.",
        bold=True,
    )

    heading(doc, "7.3. Что происходит при открытии контейнера", 2)
    bullet(
        doc,
        "VS Code собирает тот же docker-compose.yml, что и обычный запуск, плюс "
        "тонкую надстройку из .devcontainer. Это не отдельное окружение — "
        "расхождений «в контейнере работает, в бою нет» быть не может;",
    )
    bullet(
        doc,
        "контейнер web запускается с командой sleep infinity, чтобы VS Code было "
        "к чему подключиться. Поэтому сервер надо запускать вручную (раздел 4);",
    )
    bullet(
        doc,
        "папка проекта монтируется внутрь контейнера, поэтому правки в редакторе "
        "сразу видны в контейнере;",
    )
    bullet(
        doc,
        "закрытие окна VS Code останавливает контейнеры (настройка "
        "shutdownAction: stopCompose). Это защита от ситуации, когда вы ушли, а "
        "стек держит порты.",
    )

    heading(doc, "7.4. Два разработчика на одном сервере", 2)
    body(
        doc,
        "Здесь есть развилка, и её надо выбрать осознанно. Оба варианта рабочие, "
        "но ведут себя по-разному.",
    )
    body(doc, "Вариант A. Один общий набор контейнеров.", bold=True)
    body(
        doc,
        "Оба подключаются к одному и тому же контейнеру web. Просто, настраивать "
        "ничего не нужно. Подходит, когда вы работаете в разное время или вместе "
        "разбираете одну задачу.",
    )
    body(doc, "Минусы, которые надо знать заранее:")
    bullet(
        doc,
        "база данных одна на двоих: миграции и демо-данные видят оба, и "
        "«у меня всё сломалось» может означать «партнёр только что удалил таблицу»;",
    )
    bullet(
        doc,
        "сервер runserver один: если его запустили вы, второй разработчик "
        "получит Address already in use. Запускает кто-то один;",
    )
    bullet(
        doc,
        "файлы общие: правки видны сразу, конфликты возможны прямо в рабочей папке.",
    )
    body(doc, "Вариант B. У каждого свой набор контейнеров.", bold=True)
    body(
        doc,
        "Полная изоляция: свои контейнеры, своя база, свои порты. Так можно "
        "работать одновременно, не мешая друг другу. Требует несколько переменных "
        f"в файле .env на {SRV}.",
    )
    warn(
        doc,
        "У каждого разработчика должен быть свой .env, потому что переменные ниже "
        "разные. Общий .env означает общие порты и общий конфликт.",
    )
    body(doc, "Первый разработчик — значения по умолчанию, менять ничего не нужно.")
    body(doc, f"Второй разработчик добавляет в свой .env на {SRV}:")
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
        ["Переменная", "Что изолирует"],
        [
            ["COMPOSE_PROJECT_NAME", "Отдельный набор контейнеров и отдельная сеть."],
            ["ALPHA_CONTAINER_PREFIX", "Имена контейнеров: alpha2_web вместо alpha_web."],
            ["ALPHA_WEB_PORT", "Порт панели управления на сервере."],
            ["ALPHA_DB_PORT", "Порт PostgreSQL на сервере."],
            ["ALPHA_REDIS_PORT", "Порт Redis на сервере."],
        ],
    )
    body(
        doc,
        "После этого второй разработчик работает так же, но его приложение "
        "открывается на http://127.0.0.1:8081/, а база доступна на порту 5433.",
    )
    warn(
        doc,
        "Все эти порты остаются привязанными к 127.0.0.1. Менять привязку на "
        "0.0.0.0 нельзя ни при каком числе разработчиков — см. раздел 11.",
    )

    heading(doc, "7.5. Частые ошибки", 2)
    table(
        doc,
        ["Симптом", "Причина и что делать"],
        [
            [
                "Сайт недоступен на порту 8080",
                "Не тот порт. В Dev Containers приложение на 8000 (раздел 3).",
            ],
            [
                "Сайт недоступен на порту 8000",
                "Сервер не запущен. Контейнер стартует со sleep infinity (раздел 4).",
            ],
            [
                "В логе Starting development server, а браузер не открывает",
                "Запущено на 127.0.0.1 вместо 0.0.0.0 (раздел 4).",
            ],
            [
                "Address already in use",
                "Сервер уже запущен — ваш второй раз или коллегой. См. 7.4.",
            ],
            [
                "container name already in use",
                t(
                    "Два набора контейнеров с одинаковым префиксом. Задайте ",
                    "ALPHA_CONTAINER_PREFIX и COMPOSE_PROJECT_NAME (7.4).",
                ),
            ],
            [
                "Правки не видны в контейнере",
                t(
                    "Сломалось монтирование. Проверьте, что в ",
                    ".devcontainer/docker-compose.extend.yml монтируется точка, а не две точки.",
                ),
            ],
            [
                "env file .env not found",
                f"Нет файла .env на {SRV}. Выполните make env.",
            ],
        ],
    )


def section_sync(doc: Document) -> None:
    heading(doc, "8. Синхронизация рабочих окружений", 1)
    body(
        doc,
        "Когда над проектом работают несколько человек с разных ноутбуков, "
        "рассинхронизация окружений — главный источник загадочных ошибок. Правило "
        "простое: то, что можно хранить в Git, хранится в Git; то, что нельзя, "
        "синхронизируется отдельно и осознанно.",
    )

    heading(doc, "8.1. Что общее, а что личное", 2)
    table(
        doc,
        ["Что", "Где живёт", "Как синхронизируется"],
        [
            ["Код, миграции, тесты", "Git", "git pull и git push"],
            [
                ".env",
                f"Только на {SRV}, в Git не попадает",
                "Вручную, по защищённому каналу. См. 8.2",
            ],
            [
                "docker-compose.yml, .devcontainer",
                "Git",
                "git pull — одинаково у всех автоматически",
            ],
            [
                "Расширения VS Code",
                ".vscode/extensions.json в Git",
                "VS Code предложит установить при открытии папки",
            ],
            [
                "Личные настройки VS Code",
                ".vscode/settings.json — в Git не попадает",
                "Не синхронизируются, и это нормально",
            ],
            [
                "База данных и Redis",
                "Тома Docker на сервере",
                "Общие для варианта A, свои для варианта B",
            ],
        ],
    )

    heading(doc, "8.2. Синхронизация .env", 2)
    body(
        doc,
        "Файл .env содержит пароли базы данных, Redis, Django и ключ API. В Git "
        "он не попадает и не должен. При этом у всех участников значения должны "
        "совпадать — иначе один видит базу, а другой нет.",
    )
    body(doc, "Как правильно передать .env новому разработчику:", bold=True)
    numbered(doc, f"Скопировать файл с {SRV} во временное место.")
    numbered(doc, "Передать через защищённый канал: scp по SSH или менеджер паролей.")
    numbered(doc, "После передачи временную копию удалить.")
    code(
        doc,
        f"""# с ноутбука разработчика
scp {SSH_HOST}:/root/projects/AlphaAgent/.env ./env.backup
# передать коллеге защищённым каналом (не почтой, не мессенджером)
# после — удалить временные копии
rm ./env.backup""",
    )
    warn(
        doc,
        "Никогда не отправляйте .env по электронной почте, в мессенджере или в "
        "задаче трекера. Это файл с паролями. Если он туда попал — считайте пароли "
        "скомпрометированными и смените их.",
    )
    body(doc, "Проверить, что .env не попал в Git (на сервере или на ноутбуке):")
    code(
        doc,
        """git check-ignore -v .env                # должен показать строку из .gitignore
git log --all --full-history -- .env    # должно быть пусто""",
    )
    body(
        doc,
        "Если вторая команда что-то вернула, файл когда-то был закоммичен. Смены "
        "паролей тогда недостаточно: значение осталось в истории. См. раздел 11.5.",
    )

    heading(doc, "8.3. Синхронизация расширений и настроек VS Code", 2)
    body(
        doc,
        "Файл .vscode/extensions.json хранится в Git. Когда вы открываете папку, "
        "VS Code сам предлагает установить перечисленные расширения — это и есть "
        "синхронизация, делать ничего не нужно.",
    )
    body(
        doc,
        "Файл .vscode/settings.json намеренно не хранится в Git: у каждого свои "
        "шрифт, тема и размер отступов. На работу проекта это не влияет.",
        italic=True,
    )
    body(
        doc,
        "Настройки внутри контейнера задаются в .devcontainer/devcontainer.json и "
        "применяются у всех одинаково при сборке контейнера.",
    )

    heading(doc, "8.4. Гигиена Git при работе вдвоём", 2)
    bullet(doc, "Перед началом работы: git pull. Перед уходом: git push.")
    bullet(
        doc,
        "Не работайте вдвоём в одной ветке на одном сервере без нужды. Разные "
        "ветки — разные задачи, конфликтов меньше.",
    )
    bullet(
        doc,
        "Никогда не делайте git add -A не глядя. Эта команда затягивает в коммит "
        "всё, включая .env, если он случайно не в .gitignore. Смотрите git status "
        "перед коммитом.",
    )
    bullet(
        doc,
        "Файлы состояния вроде celerybeat-schedule меняются сами. Они в "
        ".gitignore, и это правильно: их не надо ни коммитить, ни чинить.",
    )


def section_laptops(doc: Document) -> None:
    heading(doc, "9. Один разработчик, несколько ноутбуков", 1)
    body(
        doc,
        "Ситуация: вы работаете то с одного ноутбука, то с другого, и оба "
        f"подключаются к одному и тому же {SERVER}. Хорошая новость: почти ничего "
        f"не нужно делать по-особому, потому что код и окружение живут на сервере, "
        f"а не на ноутбуке.",
    )
    body(doc, "Что нужно на каждом новом ноутбуке:", bold=True)
    table(
        doc,
        ["Шаг", "Зачем"],
        [
            ["VS Code, Remote-SSH, Dev Containers", "Без них нет подключения"],
            [
                "Свой SSH-ключ для этого ноутбука",
                "Раздел 5. Не копируйте ключ с другого ноутбука",
            ],
            ["Строка в ~/.ssh/config", "Раздел 5.3"],
            ["Ничего больше", "Git, Python и Docker на ноутбуке не нужны"],
        ],
    )
    warn(
        doc,
        "Заведите отдельный ключ на каждый ноутбук, а не один общий. Если ноутбук "
        "потерян, вы удаляете одну строку из authorized_keys — и доступ теряет "
        "только он, а не все ваши машины.",
    )
    body(doc, "Как отозвать доступ с потерянного ноутбука:", bold=True)
    code(
        doc,
        """# на сервере: посмотреть, какие ключи есть
cat ~/.ssh/authorized_keys
# удалить строку потерянного ноутбука (осторожно, не удалите остальные)
nano ~/.ssh/authorized_keys""",
    )
    body(
        doc,
        "Чтобы различать ключи, указывайте комментарий при создании: "
        'ssh-keygen -C "ноутбук-имя-дата". Он виден в конце строки в authorized_keys.',
        italic=True,
    )
    body(doc, "Что не надо делать:", bold=True)
    bullet(
        doc,
        "копировать папку проекта на ноутбук и работать локально — тогда появятся "
        "две расходящиеся копии;",
    )
    bullet(doc, "копировать закрытый SSH-ключ между ноутбуками;")
    bullet(
        doc,
        "заводить на ноутбуке свой Docker и свою базу — они не будут знать о данных на сервере.",
    )
    body(
        doc,
        "Исключение — если вы сознательно хотите работать офлайн. Тогда см. "
        "раздел 10: там описано, как получить копию, но это отдельный режим, а не "
        "обычный.",
        italic=True,
    )


def section_clone(doc: Document) -> None:
    heading(doc, "10. Клонирование репозитория на ноутбук", 1)
    body(
        doc,
        "В обычных условиях программировать на ноутбуке не нужно: код и окружение "
        "живут на сервере. Но копия на ноутбуке бывает полезна — посмотреть код без "
        "подключения, сделать быструю правку в документации, или просто иметь "
        "резервную копию.",
    )
    warn(
        doc,
        "Клонирование даёт вам файлы, по сути, только для чтения. Если вы начнёте "
        "править их на ноутбуке и коммитить, а параллельно то же самое делать на "
        "сервере, получите расходящиеся истории и конфликты. Рабочее место — сервер.",
    )

    heading(doc, "10.1. Клонирование", 2)
    body(
        doc,
        "Клонировать нужно из GitHub, а не с сервера. GitHub — источник правды; "
        "сервер это просто машина, где проект запущен.",
    )
    code(
        doc,
        """# на ноутбуке
git clone https://github.com/SergeyGer/AlphaAgent.git
cd AlphaAgent""",
    )
    body(
        doc,
        "Если репозиторий приватный, понадобится доступ к GitHub: либо вход через "
        "браузер (VS Code и Git Credential Manager это умеют), либо personal "
        "access token. Токен хранится в менеджере учётных данных, а не в файле "
        "внутри проекта.",
    )

    heading(doc, "10.2. Чего в репозитории нет", 2)
    body(
        doc,
        "Часть файлов намеренно не хранится в Git. После клонирования их не "
        "будет, и это ожидаемо, а не поломка.",
    )
    table(
        doc,
        ["Файл", "Почему не в Git", "Откуда взять"],
        [
            [
                ".env",
                "Содержит пароли и ключи API",
                f"С {SRV}: scp {SSH_HOST}:/root/projects/AlphaAgent/.env .",
            ],
            [
                "Run_Stop_Project.docx",
                "Личный справочник, а не часть проекта",
                "Этот документ. См. 10.3",
            ],
            [
                ".vscode/settings.json",
                "Личные настройки редактора",
                "Не нужен, VS Code создаст свой",
            ],
            [
                "celerybeat-schedule*",
                "Состояние планировщика, меняется само",
                "Появится при запуске",
            ],
            [
                "node_modules, venv, htmlcov",
                "Артефакты сборки",
                "Создаются командами установки",
            ],
        ],
    )

    heading(doc, "10.3. Как хранить .env и Run_Stop_Project.docx на ноутбуках", 2)
    body(
        doc,
        "Оба файла нужны на каждом ноутбуке, но в Git им нельзя. Практичный "
        "подход — отдельное место, которое синхронизируется не через Git проекта.",
    )
    body(doc, "Вариант 1. Приватный Git-репозиторий для личных файлов.", bold=True)
    code(
        doc,
        """alphaagent-private/
    .env
    Run_Stop_Project.docx
    README.md          # что это и почему здесь""",
    )
    body(
        doc,
        "Отдельный приватный репозиторий, куда кладутся именно эти файлы. Плюс: "
        "версионирование и доступ с любого ноутбука. Минус: .env теперь лежит в "
        "Git, пусть и в приватном. Приемлемо только если вы понимаете риск и "
        "включили двухфакторную аутентификацию на GitHub.",
    )
    body(doc, "Вариант 2. Менеджер паролей.", bold=True)
    body(
        doc,
        "1Password, Bitwarden, KeePass — хранят .env как защищённую заметку, а "
        "документ как вложение. Плюс: не в Git вообще. Это предпочтительный "
        "вариант для .env.",
    )
    body(doc, "Вариант 3. Копия на сервере и scp по требованию.", bold=True)
    body(
        doc,
        f"Источник правды для .env — {SRV}. На новый ноутбук файл забирается "
        f"одной командой, когда он действительно нужен:",
    )
    code(doc, f"scp {SSH_HOST}:/root/projects/AlphaAgent/.env ./")
    body(
        doc,
        "Для Run_Stop_Project.docx этот вариант тоже работает: документ лежит на "
        "сервере в корне проекта и не попадает в Git, поэтому его можно забрать "
        "так же.",
    )
    warn(
        doc,
        f"Какой бы вариант вы ни выбрали, у файла .env должна быть резервная "
        f"копия вне {SRV}. Если сервер потерян, а .env был только на нём, пароли "
        f"придётся восстанавливать по памяти.",
    )


def section_security(doc: Document) -> None:
    heading(doc, "11. Безопасность", 1)
    body(
        doc,
        f"Сервер {SERVER_IP} имеет публичный IP-адрес, то есть его видят сканеры "
        f"всего интернета круглосуточно. Ниже — что уже сделано и что стоит "
        f"сделать.",
    )

    heading(doc, "11.1. Что уже закрыто", 2)
    table(
        doc,
        ["Что", "Состояние"],
        [
            ["PostgreSQL 5432", "Только 127.0.0.1 — из интернета недоступен"],
            ["Redis 6379", "Только 127.0.0.1 и с паролем"],
            ["Вход по паролю SSH", "Отключён, только ключи"],
            ["DeepSeek Harness 3080", "Только 127.0.0.1, доступ через туннель"],
            [
                "Панель управления 8080",
                "Доступна с сервера; Django отсекает чужие Host",
            ],
        ],
    )
    body(
        doc,
        "Это проверено и закреплено автотестом: если кто-то снова опубликует базу "
        "в интернет, сборка упадёт.",
        italic=True,
    )

    heading(doc, "11.2. fail2ban — защита от перебора", 2)
    body(
        doc,
        "Даже с отключённым паролем SSH кто-то будет стучаться. fail2ban читает "
        "логи и временно блокирует адреса, с которых идут неудачные попытки.",
    )
    body(doc, f"Установка (на {SRV}, от root):", bold=True)
    code(
        doc,
        """apt update && apt install -y fail2ban
systemctl enable --now fail2ban""",
    )
    body(doc, "Настройка в файле /etc/fail2ban/jail.local:", bold=True)
    code(
        doc,
        """[DEFAULT]
# Блокировка на 1 час после 5 неудач в течение 10 минут
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = %(sshd_backend)s""",
    )
    body(doc, "Применить и проверить:", bold=True)
    code(
        doc,
        """systemctl restart fail2ban
fail2ban-client status sshd""",
    )
    body(
        doc,
        "Вывод покажет число заблокированных адресов. Если оно растёт, защита "
        "работает и отсекает автоматические попытки.",
    )
    warn(
        doc,
        "Не ставьте bantime слишком большим и не блокируйте навсегда: если вы "
        "ошиблись паролем с нового ноутбука, заблокируете себя. Начинайте с "
        "bantime 1h и maxretry 5.",
    )
    body(doc, "Разблокировать свой адрес, если всё-таки попали под блокировку:")
    code(doc, "fail2ban-client set sshd unbanip ВАШ.IP.АДРЕС")

    heading(doc, "11.3. Межсетевой экран (ufw)", 2)
    body(
        doc,
        f"Сейчас ufw на {SRV} не активен. Порт 8080 открыт наружу, и это "
        f"осознанное решение — но лучше ограничить круг того, что вообще видно.",
    )
    body(doc, "Минимальная настройка, которая не отрежет вас от сервера:", bold=True)
    code(
        doc,
        """# СНАЧАЛА разрешить SSH, иначе потеряете доступ
ufw allow 22/tcp
# Затем включить
ufw --force enable
ufw status verbose""",
    )
    warn(
        doc,
        "Порядок обязателен: сначала ufw allow 22/tcp, только потом enable. Если "
        "включить раньше, SSH-соединение оборвётся и войти будет нечем — "
        "понадобится консоль хостера.",
    )
    body(
        doc,
        "Нужен ли 8080 снаружи — решать вам. Если вы работаете только через VS "
        "Code, он не нужен: проброс порта работает поверх SSH. Тогда 8080 тоже "
        "можно закрыть:",
    )
    code(
        doc,
        """ufw deny 8080/tcp
# и дополнительно привязать панель к localhost:
# в .env   ALPHA_WEB_PORT=127.0.0.1:8080""",
    )

    heading(doc, "11.4. Docker и права", 2)
    bullet(
        doc,
        "Не добавляйте пользователей в группу docker без необходимости: членство "
        "в ней равноценно root, потому что позволяет смонтировать любой каталог "
        "хоста в контейнер.",
    )
    bullet(
        doc,
        "Базы данных держите на 127.0.0.1. Это уже сделано и проверяется тестом.",
    )
    bullet(
        doc,
        "Не публикуйте порт 2375 (Docker API) ни при каких обстоятельствах.",
    )
    bullet(
        doc,
        "Обновляйте образы: docker compose pull и пересборка раз в месяц. "
        "Устаревший образ — самая частая причина взлома.",
    )

    heading(doc, "11.5. Секреты", 2)
    table(
        doc,
        ["Правило", "Почему"],
        [
            [
                ".env не попадает в Git никогда",
                "В нём пароли базы, Redis, Django и ключ API",
            ],
            [
                "Секрет, попавший в историю Git, считается утёкшим",
                "Он остаётся в старых коммитах даже после удаления файла",
            ],
            [
                "Токен DeepSeek Harness — как пароль",
                "Он даёт полный доступ к агенту",
            ],
            [
                "Ключ API Anthropic нельзя класть в код",
                "Только в .env, и только на сервере",
            ],
            [
                "Права на .env — 600",
                "Чтобы прочитать мог только владелец: chmod 600 .env",
            ],
        ],
    )
    body(doc, "Если секрет всё же попал в Git, порядок действий:")
    numbered(doc, "Немедленно сменить сам секрет — это главное.")
    numbered(doc, "Удалить его из истории: git filter-repo или BFG Repo-Cleaner.")
    numbered(doc, "Проверить, не пользовался ли им кто-то ещё (логи провайдера).")
    numbered(
        doc,
        "Только после этого успокоиться: удаление из истории без смены секрета бесполезно.",
    )
    body(doc, "Проверка, что сейчас ничего лишнего не отслеживается:")
    code(
        doc,
        """git ls-files | grep -E "\\.env$|token|secret|key" || echo "чисто\"""",
    )


def section_commands(doc: Document) -> None:
    heading(doc, "12. Полезные команды", 1)
    table(
        doc,
        ["Задача", "Команда"],
        [
            ["Запустить всё", "make start"],
            ["Остановить, оставив собранным", "make stop"],
            ["Остановить и освободить порты", "make down"],
            ["Посмотреть, что работает", "make status"],
            ["Логи всех сервисов", "make logs"],
            ["Консоль Django", "make shell"],
            ["Тесты", "make test"],
            ["Линтер", "make lint"],
            ["Всё сразу: линт, проверки, тесты", "make check"],
            ["Войти на сервер", f"ssh {SSH_HOST}"],
            [
                "Туннель к DeepSeek Harness",
                f"ssh -N -L {DSH_PORT}:127.0.0.1:{DSH_PORT} {SSH_HOST}",
            ],
            [
                "Адрес DeepSeek Harness с токеном",
                f"journalctl -u {DSH_UNIT} -n 40 | grep 'dsh web:' | tail -1",
            ],
            ["Статус DeepSeek Harness", f"systemctl status {DSH_UNIT}"],
            ["Статус fail2ban", "fail2ban-client status sshd"],
            ["Статус файрвола", "ufw status verbose"],
        ],
    )


def section_troubleshooting(doc: Document) -> None:
    heading(doc, "13. Если что-то не работает", 1)
    table(
        doc,
        ["Сообщение или симптом", "Что делать"],
        [
            [
                "port is already allocated",
                t(
                    "Порт занят другим проектом. make down здесь или в другом проекте. ",
                    "Либо сменить порт в .env (ALPHA_WEB_PORT и другие).",
                ),
            ],
            [
                "env file .env not found",
                f"Нет .env на {SRV}. Выполните make env.",
            ],
            [
                "Cannot connect to the Docker daemon",
                t(
                    "Docker не запущен: systemctl start docker на сервере, либо ",
                    "запустите Docker Desktop на Windows.",
                ),
            ],
            [
                "Страница не открывается сразу после старта",
                "Подождите 30–40 секунд: база должна стать готовой. make status.",
            ],
            [
                "Permission denied (publickey)",
                "Права 700/600 на ~/.ssh и authorized_keys, либо ключ не тот (раздел 5.4).",
            ],
            [
                "Address already in use внутри контейнера",
                "Сервер уже запущен — ваш второй раз или коллегой (раздел 7.4).",
            ],
            [
                "DeepSeek Harness: authentication required",
                "Ссылка без токена или токен устарел. Возьмите свежий из журнала (раздел 6.2).",
            ],
            [
                "DeepSeek Harness не открывается совсем",
                "Туннель не поднят. Проверьте, что окно с ssh -N -L открыто (раздел 6.2).",
            ],
        ],
    )


def section_cheatsheet(doc: Document) -> None:
    heading(doc, "14. Шпаргалка", 1)
    body(doc, "Если запомнить только это — уже можно работать:", bold=True)
    code(
        doc,
        """# работа
ssh Hetzner
cd ~/projects/AlphaAgent
make start

# браузер (на сервере)
http://127.0.0.1:8080/

# закончили
make down""",
    )
    body(doc, "Через Dev Containers:", bold=True)
    code(
        doc,
        """Ctrl+Shift+P -> Dev Containers: Reopen in Container
Ctrl+Shift+P -> Tasks: Run Task -> AlphaAgent: Run server (in this container)
# браузер: http://127.0.0.1:8000/
# порт в браузере смотрим на вкладке PORTS""",
    )
    body(doc, "DeepSeek Harness:", bold=True)
    code(
        doc,
        f"""ssh -N -L {DSH_PORT}:127.0.0.1:{DSH_PORT} Hetzner        # окно 1, оставить открытым
ssh Hetzner "journalctl -u {DSH_UNIT} -n 40 | grep 'dsh web:' | tail -1"   # окно 2
# открыть полученный адрес в браузере""",
    )
    doc.add_paragraph()
    body(
        doc,
        "Файл создан скриптом scripts/make_run_stop_doc.py. Если команды в "
        "проекте изменятся — запустите скрипт заново.",
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
