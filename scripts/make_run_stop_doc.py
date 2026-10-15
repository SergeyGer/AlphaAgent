#!/usr/bin/env python
# ruff: noqa: RUF001, RUF003
#
# RUF001-3 flag Cyrillic characters that look like Latin ones. This file is a
# Russian-language document generator, so those characters are the entire point;
# the rule is silenced HERE rather than in pyproject.toml so it keeps guarding
# every other file in the project, where a stray Cyrillic "с" in an identifier or
# a URL would be a genuine bug that is almost impossible to see.
"""Generate Run_Stop_Project.docx - the Russian-language run/stop guide.

Kept in the repository, unlike its output, so the document can be regenerated
when the commands change. The .docx itself is gitignored: it is a personal
reference for one operator, not a project artefact, and committing a binary that
Word rewrites on every open produces nothing but noise in the history.

    python scripts/make_run_stop_doc.py
"""

from __future__ import annotations

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

ACCENT = RGBColor(0x1F, 0x3A, 0x5F)
MUTED = RGBColor(0x60, 0x60, 0x60)


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
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)


def body(doc: Document, text: str, *, bold: bool = False, italic: bool = False) -> None:
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    if italic:
        run.font.color.rgb = MUTED


def bullet(doc: Document, text: str) -> None:
    doc.add_paragraph(text, style="List Bullet")


def heading(doc: Document, text: str, level: int = 1) -> None:
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = ACCENT


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


def build() -> Path:
    doc = Document()

    # Base font: Word 2016 defaults to Calibri, which renders Cyrillic well.
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    title = doc.add_heading("AlphaAgent — запуск и остановка проекта", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    body(
        doc,
        "Краткая инструкция: как поднять все контейнеры и сервисы, когда вы хотите "
        "поработать с проектом, и как всё остановить, когда вы переключаетесь на "
        "другой проект в VS Code на этой же машине.",
        italic=True,
    )

    # ---------------------------------------------------------------- 1
    heading(doc, "1. Главное в двух словах", 1)
    body(
        doc,
        "Проект состоит из шести контейнеров. Запускаются и останавливаются они "
        "одной командой — вручную запускать каждый не нужно.",
    )
    table(
        doc,
        ["Контейнер", "Что это", "Порт на вашей машине"],
        [
            ["web", "Веб-сервер: API, панель управления, WebSocket", "8080"],
            ["worker", "Celery: выполняет задачи ИИ-агента", "—"],
            ["beat", "Планировщик: запускает обходы по расписанию", "—"],
            ["mcp", "Сервер инструментов (котировки, новости)", "—"],
            ["db", "PostgreSQL 16 — база данных", "5432"],
            ["redis", "Redis 7 — очередь задач и кэш", "6379"],
        ],
    )
    body(
        doc,
        "Панель управления после запуска: http://127.0.0.1:8080/  "
        "Логин demo, пароль demo-pass-123.",
    )

    # ---------------------------------------------------------------- 2
    heading(doc, "2. Запуск проекта", 1)
    body(
        doc,
        "Три способа. Выберите тот, который удобнее на конкретном ноутбуке — результат одинаковый.",
    )

    heading(doc, "Способ 1. Из VS Code (работает одинаково на Windows и на Linux)", 2)
    body(doc, "Самый удобный вариант, потому что не требует ничего помнить:")
    bullet(doc, "Нажмите Ctrl+Shift+P")
    bullet(doc, 'Наберите "Run Task" и выберите «Tasks: Run Task»')
    bullet(doc, "Выберите «AlphaAgent: Start»")
    body(
        doc,
        "Там же есть «AlphaAgent: Stop», «AlphaAgent: Shut down», "
        "«AlphaAgent: Status» и «AlphaAgent: Logs».",
    )

    heading(doc, "Способ 2. В терминале VS Code (Linux, macOS, WSL, Git Bash)", 2)
    code(doc, "make start")

    heading(doc, "Способ 3. Напрямую через Docker (работает везде)", 2)
    code(doc, "docker compose up -d --build")

    body(
        doc,
        "Первый запуск занимает несколько минут: Docker собирает образ. "
        "Последующие — несколько секунд.",
    )

    # ---------------------------------------------------------------- 3
    heading(doc, "3. Остановка проекта — самый важный раздел", 1)
    body(
        doc,
        "Здесь есть развилка, и от неё зависит, будут ли сервисы двух проектов "
        "мешать друг другу. Команд две, и они делают разное.",
    )

    table(
        doc,
        ["Команда", "Что происходит", "Когда использовать"],
        [
            [
                "make stop\n(или задача «AlphaAgent: Stop»)",
                "Контейнеры останавливаются, но остаются собранными. Порты и память освобождаются.",
                "Короткий перерыв: вернуться к проекту через час-два. Запуск "
                "после этого — мгновенный.",
            ],
            [
                "make down\n(или задача «AlphaAgent: Shut down»)",
                "Контейнеры удаляются, сеть освобождается полностью.",
                "Переключение на другой проект надолго. Именно это освобождает "
                "порты 8080, 5432 и 6379 для другого проекта.",
            ],
        ],
    )

    body(
        doc,
        "Если вы переключаетесь на другой проект — используйте down. "
        "Остановленные контейнеры в некоторых конфигурациях Docker Desktop "
        "продолжают удерживать порты, и тогда второй проект не сможет запуститься.",
        bold=True,
    )

    body(
        doc,
        "Данные при этом не теряются: ни stop, ни down не удаляют тома. "
        "База данных и Redis сохраняются полностью.",
        italic=True,
    )

    # ---------------------------------------------------------------- 4
    heading(doc, "4. Переключение между проектами", 1)
    body(doc, "Обычный сценарий — вы работаете над двумя проектами по очереди:")
    code(
        doc,
        """# Закончили с AlphaAgent, переходите на другой проект
cd ~/projects/AlphaAgent
make down

# ... работаете над другим проектом ...

# Возвращаетесь к AlphaAgent
cd ~/projects/AlphaAgent
make start""",
    )
    body(
        doc,
        "Если другой проект тоже использует порт 8080 или 5432, конфликта не будет "
        "именно потому, что make down освободил их.",
    )

    # ---------------------------------------------------------------- 5
    heading(doc, "5. Автозапуск отключён", 1)
    body(
        doc,
        "Ни один контейнер не запускается сам. Ни после перезагрузки сервера, ни "
        "после перезапуска Docker. Это сделано намеренно:",
    )
    bullet(
        doc,
        "проект не занимает память и порты, пока вы с ним не работаете;",
    )
    bullet(
        doc,
        "автономная торговля не возобновляется без вашего ведома после перезагрузки;",
    )
    bullet(
        doc,
        "сервисы разных проектов не конкурируют за одни и те же порты.",
    )
    body(
        doc,
        "Проверить это можно так — в ответе должно быть no для всех шести контейнеров:",
    )
    code(
        doc,
        "docker inspect alpha_web --format '{{.HostConfig.RestartPolicy.Name}}'",
    )

    # ---------------------------------------------------------------- 6
    heading(doc, "6. Работа через Dev Containers", 1)
    body(
        doc,
        "В проекте есть папка .devcontainer — она настраивает расширение "
        "Dev Containers (Microsoft). Открывать проект целиком в контейнере не "
        "обязательно, но удобно: VS Code сам поднимает нужные сервисы.",
    )
    code(
        doc,
        """Ctrl+Shift+P  ->  Dev Containers: Reopen in Container""",
    )
    body(doc, "Что происходит дальше:")
    bullet(
        doc,
        "VS Code собирает стек проекта (те же шесть контейнеров) и подключается к контейнеру web;",
    )
    bullet(
        doc,
        "терминал внутри VS Code работает уже внутри контейнера — там есть Python "
        "и все зависимости;",
    )
    bullet(
        doc,
        "закрытие окна VS Code останавливает контейнеры автоматически.",
    )
    body(
        doc,
        "При первом открытии нужно один раз выполнить миграции и создать "
        "демо-данные. В терминале внутри контейнера:",
    )
    code(
        doc,
        """python manage.py migrate
python manage.py seed_demo
python manage.py runserver 0.0.0.0:8000""",
    )

    # ---------------------------------------------------------------- 7
    heading(doc, "7. Полезные команды", 1)
    table(
        doc,
        ["Задача", "Команда"],
        [
            ["Запустить всё", "make start"],
            ["Остановить, оставив собранным", "make stop"],
            ["Остановить и освободить порты", "make down"],
            ["Посмотреть, что сейчас работает", "make status"],
            ["Смотреть логи всех сервисов", "make logs"],
            ["Открыть консоль Django", "make shell"],
            ["Прогнать тесты", "make test"],
            ["Проверить код линтером", "make lint"],
        ],
    )

    # ---------------------------------------------------------------- 8
    heading(doc, "8. Если что-то не работает", 1)
    table(
        doc,
        ["Сообщение", "Что делать"],
        [
            [
                "port is already allocated",
                "Порт занят другим проектом. Выполните make down в этом проекте "
                "или docker compose down в другом. Либо смените порт: задайте "
                "ALPHA_WEB_PORT в файле .env.",
            ],
            [
                "env file .env not found",
                "Файл .env отсутствует. Выполните make env — он создаст его из "
                "шаблона .env.example.",
            ],
            [
                "Cannot connect to the Docker daemon",
                "Docker не запущен. Запустите Docker Desktop (Windows) или "
                "sudo systemctl start docker (Linux).",
            ],
            [
                "Страница не открывается",
                "Подождите 30–40 секунд после запуска: база данных должна стать "
                "готовой, и только потом стартует веб-сервер. Проверьте: "
                "make status",
            ],
        ],
    )

    # ---------------------------------------------------------------- 9
    heading(doc, "9. Шпаргалка", 1)
    body(doc, "Если запомнить только одну строку — пусть это будет она:", bold=True)
    code(
        doc,
        """make start      # начать работу
make down       # закончить и освободить порты""",
    )
    doc.add_paragraph()
    body(
        doc,
        "Файл создан автоматически скриптом scripts/make_run_stop_doc.py. "
        "Если команды в проекте изменятся — запустите скрипт заново.",
        italic=True,
    )

    doc.save(OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    path = build()
    print(f"written: {path}  ({path.stat().st_size} bytes)")
