#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launcher_tui.py — интерактивный текстовый интерфейс (curses, без зависимостей).

Движок не дублируется: всё берётся из launcher_cleanup.py, TUI только
спрашивает параметры и показывает ход работы.

Два режима работы:
  • разовый — обработать столько планшетов, сколько задано;
  • конвейер — бесконечно: обработал, попросил сменить планшет, ждёт следующий.

Запуск:  python3 launcher_tui.py
"""

from __future__ import annotations

import argparse
import sys

try:
    import curses
except ImportError:                                      # Windows без windows-curses
    sys.exit("На Windows нужен модуль curses:  pip install windows-curses\n"
             "(в Linux/macOS он идёт в стандартной библиотеке)")
import datetime
import locale
import os
import re
import shutil
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launcher_cleanup as core

TUI_VERSION = "1.7"

STAGES = [
    ("launchers", "Лаунчеры — снести заводские, оставить MDM"),
    ("browsers", "Браузеры — снести всё кроме Chrome"),
    ("assistants", "Ассистенты — Google Assistant, Bixby, ZUI AI…"),
    ("pcmode", "Режим ПК — десктопные оболочки и переключатели"),
    ("thirdparty", "Сторонние — игры и всё, что поставили дети"),
    ("extras", "Прочее — Google Meet, Google Chat"),
    ("accounts", "Аккаунты — аудит учёток и профилей"),
]

MODES = [
    ("auto", "удалить, при неудаче отключить"),
    ("uninstall", "только удалять"),
    ("disable", "только отключать"),
]

# цветовые пары
CLR_TITLE, CLR_OK, CLR_WARN, CLR_ERR, CLR_INFO, CLR_DIM, CLR_STEP, CLR_SEL, CLR_PLATE = range(1, 10)

LEVEL_PAIR = {
    "ok": CLR_OK, "warn": CLR_WARN, "err": CLR_ERR, "info": CLR_INFO,
    "step": CLR_STEP, "cmd": CLR_DIM, "raw": 0, "banner": CLR_TITLE,
}


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(CLR_TITLE, curses.COLOR_CYAN, -1)
    curses.init_pair(CLR_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(CLR_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(CLR_ERR, curses.COLOR_RED, -1)
    curses.init_pair(CLR_INFO, curses.COLOR_BLUE, -1)
    curses.init_pair(CLR_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(CLR_STEP, curses.COLOR_MAGENTA, -1)
    curses.init_pair(CLR_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(CLR_PLATE, curses.COLOR_WHITE, curses.COLOR_RED)


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """curses падает при выходе за границы — обрезаем сами."""
    height, width = win.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    try:
        win.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


# ─────────────────────────── настройки ───────────────────────────


class Options:
    def __init__(self):
        self.stages = {key: True for key, _ in STAGES}
        self.auto = True
        self.dry_run = False
        self.mode_index = 0
        self.count = 0            # 0 = бесконечный конвейер
        self.browser = core.DEFAULT_BROWSER
        self.force_unknown = False
        self.with_freeform = False
        self.lock_accounts = False
        self.restart_mdm = False
        self.adb = os.environ.get("ADB", "")
        self.adb_path = ""          # заполняется при старте поиском core.find_adb
        self.mute_notifications = True
        self.mute_stores = True
        self.remove_preinstalled = False
        self.allowed_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "allowed_apps.txt")
        self.install_mdm = True
        self.remove_extra_users = False
        self.mdm_perms = True
        self.apk = ""
        self.inventory = True
        self.ask_student = True
        self.inventory_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "inventory.csv")

    @property
    def mode(self) -> str:
        return MODES[self.mode_index][0]

    def selected_stages(self) -> list[str]:
        chosen = [key for key, _ in STAGES if self.stages[key]]
        return ["all"] if len(chosen) == len(STAGES) else chosen

    def to_args(self, log_path: str) -> argparse.Namespace:
        return argparse.Namespace(
            adb=self.adb_path or self.adb,
            device=None, all_devices=False, only="all", mode=self.mode,
            browser=self.browser, keep=[], list_only=False, dry_run=self.dry_run,
            auto=self.auto, force_unknown=self.force_unknown,
            with_freeform=self.with_freeform, lock_accounts=self.lock_accounts,
            no_set_home=False, no_set_owner=False, restart_mdm=self.restart_mdm,
            ignore_missing_mdm=False, log_path=log_path,
            apk=self.apk, install_mdm=self.install_mdm, mdm_perms=self.mdm_perms,
            remove_extra_users=self.remove_extra_users,
            allowed_file=self.allowed_file,
            mute_notifications=self.mute_notifications,
            mute_stores=self.mute_stores,
            remove_preinstalled=self.remove_preinstalled,
            inventory=self.inventory_path if self.inventory else "",
            ask_student=False,          # ФИО спрашивает сам TUI, до прогона
            auto_student_skip=True,
            student="", student_class="", on_duplicate="update",
        )


# ─────────────────────────── мастер настройки ───────────────────────────


class _Silent(core.Log):
    """Лог-заглушка: мастеру нужно только проверить наличие APK."""

    def __init__(self):
        super().__init__(None)

    def _send(self, *_args) -> None:
        pass

    raw = info = ok = warn = err = step = cmd = lambda self, *a, **k: None


_SILENT = _Silent()


def wizard(screen, options: Options) -> bool:
    """Экран вопросов. True — запускать, False — выход."""
    cursor = 0
    while True:
        rows = build_wizard_rows(options)
        screen.erase()
        height, width = screen.getmaxyx()

        safe_addstr(screen, 0, 2, "  WUNDER TABLET · подготовка планшетов  ",
                    curses.color_pair(CLR_SEL) | curses.A_BOLD)
        safe_addstr(screen, 1, 2,
                    f"движок launcher_cleanup v{core.VERSION} · TUI v{TUI_VERSION}",
                    curses.color_pair(CLR_DIM))
        adb_text = options.adb_path or "НЕ НАЙДЕН"
        safe_addstr(screen, 2, 2, f"adb: {adb_text}",
                    curses.color_pair(CLR_OK if options.adb_path else CLR_ERR))
        safe_addstr(screen, 3, 2, "НАСТРОЙКА ПРОГОНА", curses.color_pair(CLR_TITLE) | curses.A_BOLD)

        line = 5
        for index, (label, value, hint, kind) in enumerate(rows):
            selected = index == cursor
            attr = curses.color_pair(CLR_SEL) if selected else 0
            prefix = "  ▸ " if selected else "    "
            if kind == "header":
                safe_addstr(screen, line, 2, label,
                            curses.color_pair(CLR_TITLE) | curses.A_BOLD)
            else:
                safe_addstr(screen, line, 2, f"{prefix}{label:<46}", attr)
                value_attr = curses.color_pair(CLR_OK if value.startswith(("вкл", "да", "[x]"))
                                               else CLR_DIM)
                safe_addstr(screen, line, 52, value, value_attr | curses.A_BOLD)
                if hint and selected:
                    safe_addstr(screen, line, 70, hint, curses.color_pair(CLR_DIM))
            line += 1

        footer = ("↑↓ — выбор   ←→/Пробел — изменить   Enter — запуск   q — выход")
        safe_addstr(screen, height - 2, 2, footer, curses.color_pair(CLR_DIM))
        screen.refresh()

        key = screen.getch()
        selectable = [i for i, row in enumerate(rows) if row[3] != "header"]
        if key in (curses.KEY_UP, ord("k")):
            position = selectable.index(cursor) if cursor in selectable else 0
            cursor = selectable[(position - 1) % len(selectable)]
        elif key in (curses.KEY_DOWN, ord("j")):
            position = selectable.index(cursor) if cursor in selectable else -1
            cursor = selectable[(position + 1) % len(selectable)]
        elif key in (ord(" "), curses.KEY_RIGHT, curses.KEY_LEFT, ord("\t")):
            toggle_row(options, rows[cursor][3], forward=key != curses.KEY_LEFT)
        elif key in (curses.KEY_ENTER, 10, 13):
            if not options.selected_stages():
                continue
            return True
        elif key in (ord("q"), 27):
            return False


def build_wizard_rows(options: Options) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = [("ЭТАПЫ", "", "", "header")]
    for key, label in STAGES:
        rows.append((label, "[x]" if options.stages[key] else "[ ]", "", f"stage:{key}"))

    rows.append(("", "", "", "header"))
    rows.append(("РЕЖИМ РАБОТЫ", "", "", "header"))
    count_label = "конвейер (бесконечно)" if options.count == 0 else f"{options.count} шт."
    rows.append(("Сколько планшетов обработать", count_label,
                 "0 = меняю планшеты, пока не нажму q", "count"))
    rows.append(("Автомод (без подтверждений)", "да" if options.auto else "нет",
                 "вопросы не задаются", "auto"))
    rows.append(("Сухой прогон (ничего не менять)", "да" if options.dry_run else "нет",
                 "только показать команды", "dry"))
    rows.append(("Способ", MODES[options.mode_index][0],
                 MODES[options.mode_index][1], "mode"))

    rows.append(("", "", "", "header"))
    rows.append(("MDM-АГЕНТ", "", "", "header"))
    rows.append(("Ставить MDM, если его нет", "да" if options.install_mdm else "нет",
                 core.find_mdm_apk(argparse.Namespace(apk=options.apk),
                                   _SILENT) and "APK найден" or "APK НЕ НАЙДЕН",
                 "install"))
    rows.append(("Выдавать разрешения после установки", "да" if options.mdm_perms else "нет",
                 "appops, pm grant, deviceidle", "perms"))
    rows.append(("Удалять лишние профили ради owner",
                 "да" if options.remove_extra_users else "нет",
                 "данные профиля пропадут", "rmusers"))

    rows.append(("", "", "", "header"))
    rows.append(("ПРИЛОЖЕНИЯ ДЕТЕЙ", "", "", "header"))
    rows.append(("Глушить уведомления неразрешённых",
                 "да" if options.mute_notifications else "нет",
                 "через них и открывают игры", "mute"))
    rows.append(("Глушить уведомления магазинов",
                 "да" if options.mute_stores else "нет",
                 "Play, GetApps", "mutestores"))
    rows.append(("Сносить заводские приложения вендора",
                 "да" if options.remove_preinstalled else "нет",
                 "калькулятор, погода, заметки", "preinst"))

    rows.append(("", "", "", "header"))
    rows.append(("УЧЁТ", "", "", "header"))
    rows.append(("Вести таблицу учёта", "да" if options.inventory else "нет",
                 os.path.basename(options.inventory_path), "inventory"))
    rows.append(("Спрашивать ФИО и класс ученика", "да" if options.ask_student else "нет",
                 "Esc — пропустить для планшета", "student"))

    rows.append(("", "", "", "header"))
    rows.append(("ДОПОЛНИТЕЛЬНО", "", "", "header"))
    rows.append(("Сносить неопознанные пакеты", "да" if options.force_unknown else "нет",
                 "иначе они пропускаются", "force"))
    rows.append(("Сносить плавающую панель ZUI", "да" if options.with_freeform else "нет",
                 "com.zui.freeform.sidebar", "freeform"))
    rows.append(("Запретить гостя и смену аккаунтов", "да" if options.lock_accounts else "нет",
                 "", "lock"))
    rows.append(("Перезапускать MDM в конце", "да" if options.restart_mdm else "нет",
                 "", "restart"))
    return rows


def toggle_row(options: Options, kind: str, forward: bool = True) -> None:
    if kind.startswith("stage:"):
        key = kind.split(":", 1)[1]
        options.stages[key] = not options.stages[key]
    elif kind == "auto":
        options.auto = not options.auto
    elif kind == "dry":
        options.dry_run = not options.dry_run
    elif kind == "force":
        options.force_unknown = not options.force_unknown
    elif kind == "freeform":
        options.with_freeform = not options.with_freeform
    elif kind == "lock":
        options.lock_accounts = not options.lock_accounts
    elif kind == "restart":
        options.restart_mdm = not options.restart_mdm
    elif kind == "install":
        options.install_mdm = not options.install_mdm
    elif kind == "rmusers":
        options.remove_extra_users = not options.remove_extra_users
    elif kind == "perms":
        options.mdm_perms = not options.mdm_perms
    elif kind == "mute":
        options.mute_notifications = not options.mute_notifications
    elif kind == "mutestores":
        options.mute_stores = not options.mute_stores
    elif kind == "preinst":
        options.remove_preinstalled = not options.remove_preinstalled
    elif kind == "inventory":
        options.inventory = not options.inventory
    elif kind == "student":
        options.ask_student = not options.ask_student
    elif kind == "mode":
        step = 1 if forward else -1
        options.mode_index = (options.mode_index + step) % len(MODES)
    elif kind == "count":
        values = [0, 1, 2, 3, 5, 10, 15, 20, 30]
        step = 1 if forward else -1
        current = values.index(options.count) if options.count in values else 0
        options.count = values[(current + step) % len(values)]


# ─────────────────────────── экран работы ───────────────────────────


class TuiLog(core.Log):
    """core.Log, складывающий строки в буфер экрана."""

    def __init__(self, app: "Runner", path: str | None = None):
        super().__init__(path)
        self.app = app

    def _push(self, level: str, text: str) -> None:
        for piece in core._strip_ansi(text).split("\n"):
            self.app.push(level, piece)

    def raw(self, text: str = "") -> None:
        self._push("raw", text)
        self._write("", core._strip_ansi(text))

    def info(self, text: str) -> None:
        self._push("info", "• " + text)
        self._write("INFO", core._strip_ansi(text))

    def ok(self, text: str) -> None:
        self._push("ok", "✔ " + text)
        self._write("OK", core._strip_ansi(text))

    def warn(self, text: str) -> None:
        self._push("warn", "⚠ " + text)
        self._write("WARN", core._strip_ansi(text))

    def err(self, text: str) -> None:
        self._push("err", "✖ " + text)
        self._write("ERROR", core._strip_ansi(text))

    def step(self, text: str) -> None:
        self._push("step", "▸ " + text)
        self.app.stage = text
        self._write("STEP", core._strip_ansi(text))

    def cmd(self, text: str) -> None:
        # путь до adb в узком терминале только мешает: /usr/bin/adb -s X … → adb -s X …
        self._push("cmd", "  $ " + re.sub(r"^\S*/([\w.-]+)(?=\s)", r"\1", text))
        self._write("CMD", text)

    def banner(self, title: str, subtitle: str = "") -> None:
        self._push("banner", "── " + title)
        if subtitle:
            self._push("cmd", "   " + subtitle)
        self._write("", f"=== {title} {subtitle} ===")


def prompt_text(screen, title: str, hint: str, prefill: str = "") -> str | None:
    """Однострочный ввод. Enter — принять, Esc — пропустить (вернёт None)."""
    text = prefill
    height, width = screen.getmaxyx()
    screen.nodelay(False)
    curses.curs_set(1)
    try:
        while True:
            row = height - 5
            safe_addstr(screen, row, 2, " " * (width - 4))
            safe_addstr(screen, row + 1, 2, " " * (width - 4))
            safe_addstr(screen, row, 2, f"  {title}  ",
                        curses.color_pair(CLR_SEL) | curses.A_BOLD)
            safe_addstr(screen, row, len(title) + 8, hint, curses.color_pair(CLR_DIM))
            safe_addstr(screen, row + 1, 2, "> " + text + "_",
                        curses.color_pair(CLR_OK) | curses.A_BOLD)
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            except AttributeError:                       # сборка curses без get_wch
                code = screen.getch()
                key = chr(code) if 0 < code < 0x110000 else code
            if isinstance(key, str):
                if key in ("\n", "\r"):
                    return text.strip()
                if key == "\x1b":               # Esc — пропустить
                    return None
                if key in ("\x7f", "\b"):
                    text = text[:-1]
                elif key.isprintable():
                    text += key
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                text = text[:-1]
            elif key == curses.KEY_ENTER:
                return text.strip()
    finally:
        curses.curs_set(0)
        screen.nodelay(True)


class Runner:
    """Конвейер: ждём планшет → прогон → просим сменить → повтор."""

    def __init__(self, screen, options: Options):
        self.screen = screen
        self.options = options
        self.lines: deque[tuple[str, str]] = deque(maxlen=2000)
        self.lock = threading.Lock()
        self.stage = "подготовка"
        self.state = "ожидание"
        self.processed = 0
        self.failed = 0
        self.stop = False
        self.last_summary: core.Summary | None = None
        self.result: core.Summary | None = None
        self.view = "log"
        self.question: str | None = None
        self.answer: bool | None = None
        self.answered = threading.Event()
        self.adb = options.adb_path or options.adb
        self.adb_errors = 0
        self._warned: set[str] = set()

    # ── лог ──

    def push(self, level: str, text: str) -> None:
        with self.lock:
            self.lines.append((level, text))

    # ── подтверждения из движка ──

    def confirm(self, question: str, default: bool = False, auto: bool = False) -> bool:
        if auto or self.options.auto:
            return True
        self.question = question
        self.answer = None
        self.answered.clear()
        self.answered.wait()
        self.question = None
        return bool(self.answer)

    # ── ожидание устройств ──

    def online(self) -> list[str]:
        try:
            devices = core.list_devices(self.adb)
        except Exception as exc:                          # noqa: BLE001
            self.adb_errors += 1
            if self.adb_errors in (1, 25, 100):           # не засорять лог каждые 0.4 с
                self.push("err", f"adb не отвечает ({self.adb}): {exc}")
                self.push("info", core.adb_hint())
            return []
        self.adb_errors = 0
        waiting = [serial for serial, state in devices if state != "device"]
        if waiting and self.adb_errors == 0:
            for serial, state in devices:
                if state != "device" and serial not in self._warned:
                    self._warned.add(serial)
                    self.push("warn", f"{serial}: состояние '{state}' — "
                                      + ("разрешите отладку по USB на планшете"
                                         if state == "unauthorized"
                                         else "переподключите кабель"))
        return [serial for serial, state in devices if state == "device"]

    def wait_for_device(self, exclude: str | None = None) -> str | None:
        self.state = "ожидание планшета"
        while not self.stop:
            for serial in self.online():
                if serial != exclude:
                    return serial
            self.draw()
            if self.poll_keys():
                return None
            time.sleep(0.4)
        return None

    def wait_for_unplug(self, serial: str) -> None:
        self.state = "отключите планшет"
        while not self.stop and serial in self.online():
            self.draw()
            if self.poll_keys():
                return
            time.sleep(0.4)

    # ── основной цикл ──

    def run(self) -> None:
        previous: str | None = None
        while not self.stop:
            serial = self.wait_for_device(exclude=None)
            if serial is None:
                break

            self.view = "log"
            self.state = f"работа: {serial}"
            self.push("banner", f"═══ планшет {serial} ═══")

            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            args = self.options.to_args(os.path.join(log_dir, f"tui_{serial}_{stamp}.log"))

            if self.options.inventory:
                self.prepare_inventory(serial, args)

            summary = self.process(serial, args)
            if summary is not None:
                self.last_summary = summary
                self.processed += 1
                if summary.failed or summary.aborted:
                    self.failed += 1
                self.show_result(summary)
                self.result = summary
                self.view = "card"

            if self.options.count and self.processed >= self.options.count:
                self.state = "готово"
                self.push("ok", f"обработано планшетов: {self.processed} — план выполнен")
                break

            self.push("info", "отключите планшет и подключите следующий")
            self.wait_for_unplug(serial)
            previous = serial

        self.state = "завершено"
        self.draw()
        self.wait_key()

    def prepare_inventory(self, serial: str, args: argparse.Namespace) -> None:
        """Предупреждает о дубле и спрашивает ФИО — до начала работы с планшетом."""
        hw_serial = ""
        try:
            adb = core.Adb(binary=self.adb, serial=serial)
            hw_serial = adb.prop("ro.serialno") or serial
        except Exception:                                  # noqa: BLE001
            hw_serial = serial

        rows = core.inventory_read(self.options.inventory_path)
        index = core.inventory_find(rows, hw_serial)
        known = rows[index] if index >= 0 else None

        if known:
            who = known.get("ФИО ученика") or "ученик не указан"
            grade = known.get("класс")
            self.push("warn", f"ПЛАНШЕТ УЖЕ В ТАБЛИЦЕ: {hw_serial} · {who}"
                              + (f" · {grade}" if grade else "")
                              + f" · записан {known.get('дата', '?')}")
            self.push("info", "строка будет обновлена, дубликат не создаётся")
            args.student = known.get("ФИО ученика", "")
            args.student_class = known.get("класс", "")

        if not self.options.ask_student:
            return

        self.draw()
        name = prompt_text(
            self.screen, f"ФИО ученика · планшет {hw_serial}",
            "Enter — принять, Esc — пропустить",
            prefill=args.student,
        )
        if name is None:
            self.push("info", "ФИО пропущено — строка запишется без ученика")
            return
        args.student = name
        if not name:
            return
        grade = prompt_text(self.screen, "Класс", "Enter — принять, Esc — пропустить",
                            prefill=args.student_class)
        args.student_class = grade or ""
        self.push("ok", f"ученик: {args.student}"
                        + (f" · {args.student_class}" if args.student_class else ""))

    def process(self, serial: str, args: argparse.Namespace) -> core.Summary | None:
        stages = self.options.selected_stages()
        result: core.Summary | None = None
        original = core.confirm
        core.confirm = self.confirm
        try:
            for stage in stages:
                if self.stop:
                    break
                stage_args = argparse.Namespace(**vars(args))
                stage_args.only = stage
                log = TuiLog(self, args.log_path)
                worker = threading.Thread(
                    target=self._run_stage, args=(serial, stage_args, log), daemon=True
                )
                self._stage_result = None
                worker.start()
                while worker.is_alive():
                    self.draw()
                    self.poll_keys()
                    time.sleep(0.05)
                worker.join()
                log.close()
                if self._stage_result is not None:
                    result = self._stage_result
        finally:
            core.confirm = original
        return result

    def _run_stage(self, serial: str, args: argparse.Namespace, log: TuiLog) -> None:
        try:
            self._stage_result = core.process_device(serial, args, log)
        except Exception as exc:                          # noqa: BLE001
            log.err(f"сбой: {exc}")
            self._stage_result = None

    def show_result(self, summary: core.Summary) -> None:
        removed = (len(summary.launchers_removed) + len(summary.browsers_removed)
                   + len(summary.assistants_removed) + len(summary.desktop_removed)
                   + len(summary.extras_removed) + len(summary.thirdparty_removed))
        disabled = (len(summary.launchers_disabled) + len(summary.browsers_disabled)
                    + len(summary.assistants_disabled) + len(summary.desktop_disabled)
                    + len(summary.extras_disabled) + len(summary.thirdparty_disabled))
        self.push("raw", "")
        self.push("banner", f"ИТОГ · {summary.brand} {summary.model} · {summary.serial}")
        self.push("ok", f"удалено: {removed}   отключено: {disabled}")
        if summary.failed:
            self.push("err", f"не удалось: {', '.join(summary.failed)}")
        google = [acc for acc in summary.accounts if acc.type == "com.google"]
        if google:
            self.push("info", "Google-аккаунт: " + ", ".join(acc.name for acc in google))
        else:
            self.push("err", "GOOGLE-АККАУНТА НЕТ")
        status = {"added": "добавлен в таблицу учёта",
                  "updated": "строка в таблице обновлена",
                  "skipped": "в таблицу не записан"}.get(summary.inventory_status)
        if status:
            who = summary.student or "без ученика"
            self.push("ok" if summary.inventory_status != "skipped" else "warn",
                      f"{status} · {who}")

    # ── итоговая карточка ──

    def draw_result(self) -> None:
        """Нарисованный паспорт планшета во весь экран после прогона."""
        summary = self.result
        if summary is None:
            return
        screen = self.screen
        screen.erase()
        height, width = screen.getmaxyx()
        box_width = min(width - 4, 88)
        left = max(1, (width - box_width) // 2)

        google = [acc.name for acc in summary.accounts if acc.type == "com.google"]
        extra_users = [uid for uid, _, _ in summary.users if uid != "0"]
        owner_ok = summary.owner_component.startswith(core.MDM_PACKAGE)
        removed = (summary.launchers_removed + summary.browsers_removed
                   + summary.assistants_removed + summary.desktop_removed
                   + summary.extras_removed + summary.thirdparty_removed)
        disabled = (summary.launchers_disabled + summary.browsers_disabled
                    + summary.assistants_disabled + summary.desktop_disabled
                    + summary.extras_disabled + summary.thirdparty_disabled)

        row = [3]

        def line(text: str = "", attr: int = 0) -> None:
            safe_addstr(screen, row[0], left, "│", curses.color_pair(CLR_TITLE))
            safe_addstr(screen, row[0], left + 2, text, attr)
            safe_addstr(screen, row[0], left + box_width - 1, "│",
                        curses.color_pair(CLR_TITLE))
            row[0] += 1

        def field(label: str, value: str, pair: int = 0, bold: bool = True) -> None:
            safe_addstr(screen, row[0], left, "│", curses.color_pair(CLR_TITLE))
            safe_addstr(screen, row[0], left + 2, label, curses.color_pair(CLR_DIM))
            attr = curses.color_pair(pair) if pair else 0
            if bold:
                attr |= curses.A_BOLD
            safe_addstr(screen, row[0], left + 24, value or "—", attr)
            safe_addstr(screen, row[0], left + box_width - 1, "│",
                        curses.color_pair(CLR_TITLE))
            row[0] += 1

        def rule(left_char: str = "├", right_char: str = "┤") -> None:
            safe_addstr(screen, row[0], left,
                        left_char + "─" * (box_width - 2) + right_char,
                        curses.color_pair(CLR_TITLE))
            row[0] += 1

        def plate(text: str) -> None:
            padded = text.center(box_width - 4)
            safe_addstr(screen, row[0], left, "│", curses.color_pair(CLR_TITLE))
            safe_addstr(screen, row[0], left + 2, padded,
                        curses.color_pair(CLR_PLATE) | curses.A_BOLD)
            safe_addstr(screen, row[0], left + box_width - 1, "│",
                        curses.color_pair(CLR_TITLE))
            row[0] += 1

        # шапка экрана
        head = (f"  ПЛАНШЕТ ГОТОВ · обработано {self.processed}"
                + (f" · сбоев {self.failed}" if self.failed else "") + "  ")
        safe_addstr(screen, 0, 0, head.ljust(width - 1),
                    curses.color_pair(CLR_SEL) | curses.A_BOLD)

        rule("┌", "┐")
        title = f"{summary.brand} {summary.model}".strip() or summary.serial
        line(title, curses.color_pair(CLR_TITLE) | curses.A_BOLD)
        line(f"серийный {summary.hw_serial or summary.serial}",
             curses.color_pair(CLR_DIM))
        rule()

        field("Android", f"{summary.android}   {summary.build}", 0, bold=False)
        field("MDM-агент", "установлен" if summary.mdm_installed else "НЕ УСТАНОВЛЕН",
              CLR_OK if summary.mdm_installed else CLR_ERR)
        field("Владелец (owner)", summary.owner_component or "НЕ НАЗНАЧЕН",
              CLR_OK if owner_ok else CLR_ERR)
        field("Кто держит owner", summary.owner_label or "никто",
              CLR_OK if owner_ok else CLR_ERR)
        field("Ограничения owner", ", ".join(summary.restrictions) or "нет",
              CLR_WARN if summary.restrictions else 0, bold=False)
        field("Google-аккаунт", ", ".join(google) or "НЕТ",
              CLR_OK if google else CLR_ERR)
        field("Домашний экран", summary.home_now,
              CLR_OK if summary.home_now == core.MDM_PACKAGE else CLR_WARN)
        field("Браузер", summary.browser_now,
              CLR_OK if summary.browser_now == core.DEFAULT_BROWSER else CLR_WARN)

        rule()
        pair_by_color = {core.C.GREEN: CLR_OK, core.C.YELLOW: CLR_WARN,
                         core.C.RED: CLR_ERR, core.C.DIM: CLR_DIM}
        for label, value, color in core.stage_totals(summary):
            field(label, value[:box_width - 28], pair_by_color.get(color, 0),
                  bold=color != core.C.DIM)
        field("Итого", f"удалено {len(removed)} · отключено {len(disabled)}",
              CLR_OK if removed or disabled else 0)
        if removed:
            line("   " + ", ".join(removed)[:box_width - 8], curses.color_pair(CLR_DIM))
        if disabled:
            line("   " + ", ".join(disabled)[:box_width - 8], curses.color_pair(CLR_DIM))

        status = {"added": "добавлен в таблицу учёта",
                  "updated": "строка в таблице обновлена",
                  "skipped": "в таблицу не записан"}.get(summary.inventory_status)
        if status:
            who = summary.student or "ученик не указан"
            grade = f" · {summary.student_class}" if summary.student_class else ""
            rule()
            field("Учёт", f"{status} · {who}{grade}",
                  CLR_OK if summary.inventory_status != "skipped" else CLR_WARN,
                  bold=False)

        if not owner_ok or not google or summary.aborted:
            rule()
            line()
            if summary.aborted:
                plate(f"ПРЕРВАНО: {summary.aborted}")
            if not owner_ok:
                plate("ВЛАДЕЛЕЦ УСТРОЙСТВА (DEVICE OWNER) НЕ НАЗНАЧЕН")
            if not google:
                plate("НА ПЛАНШЕТЕ НЕТ GOOGLE-АККАУНТА")
            line()
        rule("└", "┘")

        hint = "ОТКЛЮЧИТЕ ПЛАНШЕТ И ПОДКЛЮЧИТЕ СЛЕДУЮЩИЙ"
        safe_addstr(screen, min(row[0] + 1, height - 3),
                    max(0, (width - len(hint)) // 2), hint,
                    curses.color_pair(CLR_WARN) | curses.A_BOLD)
        safe_addstr(screen, height - 2, 2, "l — показать лог   q — выход",
                    curses.color_pair(CLR_DIM))
        screen.refresh()

    # ── ввод ──

    def poll_keys(self) -> bool:
        """True — пользователь попросил выйти."""
        key = self.screen.getch()
        if key == -1:
            return False
        if self.question is not None:
            if key in (ord("y"), ord("Y"), ord("д")):
                self.answer = True
                self.answered.set()
            elif key in (ord("n"), ord("N"), ord("н")):
                self.answer = False
                self.answered.set()
            return False
        if key in (ord("q"), 27):
            self.stop = True
            return True
        if key in (ord("l"), ord("L"), ord("д")) and self.result is not None:
            self.view = "log" if self.view == "card" else "card"
        return False

    def wait_key(self) -> None:
        self.screen.nodelay(False)
        self.screen.getch()

    # ── отрисовка ──

    def draw(self) -> None:
        if self.view == "card" and self.result is not None:
            self.draw_result()
            return
        screen = self.screen
        screen.erase()
        height, width = screen.getmaxyx()

        plan = "конвейер ∞" if self.options.count == 0 else f"{self.options.count} шт."
        flags = []
        if self.options.dry_run:
            flags.append("DRY-RUN")
        if self.options.auto:
            flags.append("AUTO")
        if self.options.inventory:
            flags.append("УЧЁТ")
        header = (f"  WUNDER TABLET · {plan} · обработано {self.processed}"
                  + (f" · сбоев {self.failed}" if self.failed else "")
                  + (f" · {' '.join(flags)}" if flags else "") + "  ")
        safe_addstr(screen, 0, 0, header.ljust(width - 1),
                    curses.color_pair(CLR_SEL) | curses.A_BOLD)

        if self.adb_errors:
            self.state = "adb не отвечает"
        state_attr = curses.color_pair(
            CLR_ERR if self.adb_errors else
            CLR_WARN if self.state.startswith(("ожидание", "отключите")) else CLR_OK)
        safe_addstr(screen, 1, 2, f"● {self.state}", state_attr | curses.A_BOLD)
        safe_addstr(screen, 1, 40, f"шаг: {self.stage}", curses.color_pair(CLR_DIM))

        top = 3
        bottom = height - 3
        visible = bottom - top
        with self.lock:
            tail = list(self.lines)[-visible:]
        for index, (level, text) in enumerate(tail):
            pair = LEVEL_PAIR.get(level, 0)
            attr = curses.color_pair(pair) if pair else 0
            if level in ("step", "banner", "err"):
                attr |= curses.A_BOLD
            safe_addstr(screen, top + index, 2, text, attr)

        if self.question:
            plate = f"  {self.question}  [y/n]  "
            safe_addstr(screen, height - 3, 2, plate,
                        curses.color_pair(CLR_PLATE) | curses.A_BOLD)

        footer = "q — стоп"
        if self.state.startswith("ожидание"):
            footer = "подключите планшет по USB · q — выход"
        elif self.state.startswith("отключите"):
            footer = "отключите планшет, чтобы взять следующий · q — выход"
        safe_addstr(screen, height - 2, 2, footer, curses.color_pair(CLR_DIM))
        screen.refresh()


# ─────────────────────────── запуск ───────────────────────────


def main(screen) -> None:
    locale.setlocale(locale.LC_ALL, "")
    curses.curs_set(0)
    init_colors()
    core.C.enabled = False

    options = Options()
    options.adb_path = core.find_adb(options.adb) or ""
    screen.nodelay(False)

    if not options.adb_path:
        height, width = screen.getmaxyx()
        screen.erase()
        lines = [
            "!!!  A D B   Н Е   Н А Й Д Е Н  !!!",
            "",
            core.adb_hint(),
            "",
            "Enter — проверить ещё раз, q — выход",
        ]
        while not options.adb_path:
            screen.erase()
            for index, text in enumerate(lines):
                pad = max(0, (width - len(text)) // 2)
                attr = curses.color_pair(CLR_PLATE) | curses.A_BOLD if index == 0 else 0
                safe_addstr(screen, height // 2 - 3 + index, pad, text, attr)
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), 27):
                return
            options.adb_path = core.find_adb(options.adb) or ""

    if not wizard(screen, options):
        return

    screen.nodelay(True)
    runner = Runner(screen, options)
    runner.run()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        print("прервано")
