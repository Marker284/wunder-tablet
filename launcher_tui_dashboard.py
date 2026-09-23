#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launcher_tui_dashboard.py — расширенный дашборд-интерфейс оператора (TUI v3).

В отличие от линейного мастера в launcher_tui.py, этот интерфейс выполнен
в стиле Mission Control / операторского пульта:
  • Двухпанельный живой дашборд: карточка устройства, матрица этапов и лог
  • Наглядные шкалы: батарея устройства, общий прогресс прогона
  • Интерактивные модальные окна:
      [S] — Настройка этапов и параметров (чекбоксы по категориям)
      [P] — Паспорт устройства / спецификации последнего прогона
      [I] — Просмотр таблицы учёта (inventory.csv) прямо в терминале
      [D] — Список всех подключённых устройств и выбор активного
      [C] — Переключение режима конвейера (бесконечный / разовый)
      [Space] — Запуск / пауза обработки

Движок и логика берутся из launcher_cleanup.py без дублирования.

Запуск:  python3 launcher_tui_dashboard.py
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

try:
    import curses
except ImportError:
    sys.exit("На Windows нужен модуль curses:  pip install windows-curses\n"
             "(в Linux/macOS он идёт в стандартной библиотеке)")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launcher_cleanup as core

DASHBOARD_VERSION = "3.0 Pro"

# Список этапов с человекочитаемыми названиями и краткими описаниями
STAGE_DEFINITIONS = [
    ("launchers", "Лаунчеры", "снос заводских, закрепление MDM"),
    ("browsers", "Браузеры", "снос лишних, Chrome по умолчанию"),
    ("assistants", "Ассистенты", "Google Assistant, Bixby, ZUI AI"),
    ("pcmode", "Режим ПК", "десктопные оболочки и флаги"),
    ("thirdparty", "Сторонние", "игры детей вне белого списка"),
    ("stores", "Магазины", "выключение вендорских сторов и Play"),
    ("extras", "Прочее", "Meet, Duo, Google Chat"),
    ("mac", "MAC-адрес", "фиксация реального MAC, без рандома"),
    ("system", "Система", "русский язык, автовремя, замок"),
    ("accounts", "Аккаунты", "аудит Google/Mi/Samsung аккаунтов"),
]

# Цветовые пары
(
    C_DEFAULT,
    C_HEADER,
    C_ACCENT,
    C_OK,
    C_WARN,
    C_ERR,
    C_INFO,
    C_DIM,
    C_STAGE_ACTIVE,
    C_STAGE_DONE,
    C_SEL,
    C_PANEL_BOX,
    C_GAUGE_HIGH,
    C_GAUGE_MID,
    C_GAUGE_LOW,
) = range(1, 16)


def init_dashboard_colors() -> None:
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(C_HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(C_ACCENT, curses.COLOR_BLUE, -1)
    curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_ERR, curses.COLOR_RED, -1)
    curses.init_pair(C_INFO, curses.COLOR_CYAN, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_STAGE_ACTIVE, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    curses.init_pair(C_STAGE_DONE, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_PANEL_BOX, curses.COLOR_CYAN, -1)
    curses.init_pair(C_GAUGE_HIGH, curses.COLOR_GREEN, -1)
    curses.init_pair(C_GAUGE_MID, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_GAUGE_LOW, curses.COLOR_RED, -1)


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    avail = max(0, w - x - 1)
    if avail <= 0:
        return
    try:
        win.addnstr(y, x, text, avail, attr)
    except curses.error:
        pass


@dataclass
class DeviceTelemetry:
    serial: str = ""
    brand: str = "—"
    model: str = "—"
    android: str = "—"
    api: str = "—"
    battery_level: int = -1
    battery_status: str = "—"
    device_owner: str = "не назначен"
    google_account: str = "—"
    online: bool = False
    status_text: str = "Ожидание подключения..."


CONFIG_PATH = os.path.expanduser("~/.config/wunder-tablet/dashboard.json")


def load_dashboard_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_dashboard_config(options: DashboardOptions) -> None:
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        data = {
            "stages": options.stages,
            "mode": options.mode,
            "conveyor": options.conveyor,
            "inventory": options.inventory,
            "keep_play": options.keep_play,
            "lock_settings": options.lock_settings,
            "block_stores": options.block_stores,
            "reinstall_mdm": options.reinstall_mdm,
            "dry_run": options.dry_run,
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@dataclass
class DashboardOptions:
    adb_path: str = ""
    stages: dict[str, bool] = field(
        default_factory=lambda: {key: True for key, _, _ in STAGE_DEFINITIONS}
    )
    mode: str = "auto"              # auto | disable | uninstall
    conveyor: bool = True           # бесконечный конвейер
    inventory: bool = True          # вести inventory.csv
    keep_play: bool = False         # оставить Google Play включённым
    lock_settings: bool = False     # блокировать настройки
    block_stores: bool = False      # сносить магазины в thirdparty
    reinstall_mdm: str = "auto"     # auto | force | never — обновление MDM
    dry_run: bool = False           # сухой прогон

    def to_cleanup_args(self, serial: str, log_path: str) -> argparse.Namespace:
        # Конкретный этап проставляет конвейер (stage_args.only), здесь только
        # безопасное значение по умолчанию: движок ждёт строку, не список.
        only = "all"

        return argparse.Namespace(
            adb=self.adb_path,
            device=serial,
            all_devices=False,
            only=only,
            mode=self.mode,
            browser=core.DEFAULT_BROWSER,
            keep=[],
            list_only=False,
            dry_run=self.dry_run,
            auto=True,
            force_unknown=False,
            with_freeform=False,
            lock_accounts=False,
            lock_settings=self.lock_settings,
            block_stores=self.block_stores,
            list_stores=False,
            disable_stores=False,
            enable_stores=False,
            remove_stores=False,
            include_play=False,
            keep_play=self.keep_play,
            stores_catalog_only=False,
            keep_stores=False,
            set_locale=True,
            locale=core.SYSTEM_LOCALE,
            auto_time=True,
            no_set_home=False,
            no_set_owner=False,
            restart_mdm=False,
            ignore_missing_mdm=False,
            log_path=log_path,
            apk="",
            install_mdm=True,
            mdm_perms=True,
            reinstall_mdm=self.reinstall_mdm,
            remove_extra_users=False,
            allowed_file=os.path.join(os.path.dirname(__file__), "allowed_apps.txt"),
            fix_mac=True,
            mute_notifications=True,
            mute_stores=True,
            remove_preinstalled=False,
            inventory=os.path.join(os.path.dirname(__file__), "inventory.csv") if self.inventory else "",
            ask_student=False,
            auto_student_skip=True,
            student="",
            student_class="",
            on_duplicate="update",
        )


class StdoutCatcher:
    """Перехватывает любой print() в stdout/stderr и перенаправляет в очередь логов,
    чтобы в curses терминал не улетали сырые символы и не сдвигали экран."""

    def __init__(self, msg_queue: queue.Queue):
        self.queue = msg_queue
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = core._strip_ansi(line).strip("\r")
            if line:
                self.queue.put(("log", "raw", line))
        return len(s)

    def flush(self) -> None:
        if self._buf:
            line = core._strip_ansi(self._buf).strip("\r")
            if line:
                self.queue.put(("log", "raw", line))
            self._buf = ""


class DashboardLogger(core.Log):
    """Потокобезопасный логгер, отправляющий события в очередь дашборда БЕЗ вызова print()."""

    def __init__(self, msg_queue: queue.Queue, file_path: str | None = None):
        super().__init__(file_path)
        self.queue = msg_queue

    def _push(self, level: str, text: str) -> None:
        for piece in core._strip_ansi(text).split("\n"):
            piece = piece.strip("\r")
            if piece:
                self.queue.put(("log", level, piece))

    def raw(self, text: str = "") -> None:
        self._push("raw", text)
        self._write("", core._strip_ansi(text))

    def info(self, text: str) -> None:
        self._push("info", text)
        self._write("INFO", core._strip_ansi(text))

    def ok(self, text: str) -> None:
        self._push("ok", text)
        self._write("OK", core._strip_ansi(text))

    def warn(self, text: str) -> None:
        self._push("warn", text)
        self._write("WARN", core._strip_ansi(text))

    def err(self, text: str) -> None:
        self._push("err", text)
        self._write("ERROR", core._strip_ansi(text))

    def step(self, text: str) -> None:
        self._push("step", text)
        self._write("STEP", core._strip_ansi(text))

    def cmd(self, text: str) -> None:
        # Убираем длинный путь к бинарнику adb для аккуратного вывода в окно
        short_cmd = re.sub(r"^\S*/([\w.-]+)(?=\s)", r"\1", text)
        self._push("cmd", short_cmd)
        self._write("CMD", text)

    def banner(self, title: str, subtitle: str = "") -> None:
        self._push("banner", title)
        if subtitle:
            self._push("cmd", subtitle)
        self._write("", f"=== {title} {subtitle} ===")

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        if not rows:
            return
        self._push("info", " | ".join(headers))
        for r in rows:
            self._push("raw", " | ".join(r))

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass


class DashboardApp:
    def __init__(self, stdscr):
        self.screen = stdscr
        self.options = DashboardOptions()
        saved = load_dashboard_config()
        if saved:
            if "stages" in saved and isinstance(saved["stages"], dict):
                self.options.stages.update(saved["stages"])
            if "mode" in saved:
                self.options.mode = saved["mode"]
            if "conveyor" in saved:
                self.options.conveyor = bool(saved["conveyor"])
            if "inventory" in saved:
                self.options.inventory = bool(saved["inventory"])
            if "keep_play" in saved:
                self.options.keep_play = bool(saved["keep_play"])
            if "lock_settings" in saved:
                self.options.lock_settings = bool(saved["lock_settings"])
            if "block_stores" in saved:
                self.options.block_stores = bool(saved["block_stores"])
            if saved.get("reinstall_mdm") in ("auto", "force", "never"):
                self.options.reinstall_mdm = saved["reinstall_mdm"]
            if "dry_run" in saved:
                self.options.dry_run = bool(saved["dry_run"])

        self.options.adb_path = core.find_adb() or "adb"

        self.telemetry = DeviceTelemetry()
        self.active_serial: str | None = None
        self.connected_serials: list[str] = []

        self.log_lines: deque[tuple[str, str, str]] = deque(maxlen=600)  # (time, level, text)
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_lines.append((now_str, "info", "Пауза: нажмите [Пробел] для старта конвейера или [S] для настроек."))
        if saved:
            self.log_lines.append((now_str, "ok", f"Загружены сохранённые настройки из {os.path.basename(CONFIG_PATH)}"))

        self.event_queue: queue.Queue = queue.Queue()

        self.running_stage_idx: int = -1
        self.stage_states: dict[str, str] = {k: "idle" for k, _, _ in STAGE_DEFINITIONS}  # idle, running, ok, warn, err, skip
        self.stage_notes: dict[str, str] = {k: "" for k, _, _ in STAGE_DEFINITIONS}

        self.processed_count: int = 0
        self.errors_count: int = 0
        self.is_processing: bool = False
        self.paused: bool = True
        self.should_exit: bool = False

        self.last_summary: core.Summary | None = None
        self.active_modal: str | None = None  # None | "stages" | "passport" | "inventory" | "devices"
        self.modal_selection: int = 0
        self.modal_scroll: int = 0

        self.student_name: str = ""
        self.student_class: str = ""

        self.worker_thread: threading.Thread | None = None
        self.poller_thread: threading.Thread | None = None

    def start(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.nodelay(True)
        init_dashboard_colors()

        # Полный перехват sys.stdout/sys.stderr, чтобы никакой print() не просачивался в curses
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        catcher = StdoutCatcher(self.event_queue)
        sys.stdout = catcher
        sys.stderr = catcher

        try:
            # Поток фонового опроса ADB устройств
            self.poller_thread = threading.Thread(target=self._device_poller, daemon=True)
            self.poller_thread.start()

            # Поток конвейера / исполнителя
            self.worker_thread = threading.Thread(target=self._pipeline_worker, daemon=True)
            self.worker_thread.start()

            # Главный UI цикл
            while not self.should_exit:
                self._drain_queue()
                self._handle_input()
                self._render()
                time.sleep(0.04)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    def _drain_queue(self) -> None:
        while not self.event_queue.empty():
            try:
                event = self.event_queue.get_nowait()
                kind = event[0]
                now_str = datetime.datetime.now().strftime("%H:%M:%S")

                if kind == "log":
                    _, level, text = event
                    self.log_lines.append((now_str, level, text))
                elif kind == "banner":
                    _, title, sub = event
                    self.log_lines.append((now_str, "banner", f"=== {title} {sub} ==="))
                elif kind == "stage_update":
                    _, stage, state, note = event
                    self.stage_states[stage] = state
                    self.stage_notes[stage] = note
                elif kind == "telemetry":
                    _, telem = event
                    self.telemetry = telem
                elif kind == "summary":
                    _, summ = event
                    self.last_summary = summ
            except queue.Empty:
                break

    # ──────────────────────── Фоновые потоки ────────────────────────

    def _device_poller(self) -> None:
        """Регулярно опрашивает adb devices и собирает первичные метрики."""
        last_poll_error = ""
        while not self.should_exit:
            try:
                devs = core.list_devices(self.options.adb_path)
                online_serials = [s for s, state in devs if state == "device"]
                self.connected_serials = online_serials

                if not online_serials:
                    if self.telemetry.online:
                        self.event_queue.put((
                            "telemetry",
                            DeviceTelemetry(status_text="Ожидание подключения планшета..."),
                        ))
                elif not self.is_processing:
                    target = online_serials[0]
                    # Быстрый опрос характеристик
                    adb = core.Adb(binary=self.options.adb_path, serial=target)
                    brand = adb.prop("ro.product.brand") or "?"
                    model = adb.prop("ro.product.model") or "?"
                    release = adb.prop("ro.build.version.release") or "?"
                    sdk = adb.prop("ro.build.version.sdk") or "?"

                    # Батарея
                    batt_out = adb.shell("dumpsys battery", timeout=5)
                    lvl_m = re.search(r"level:\s*(\d+)", batt_out)
                    level = int(lvl_m.group(1)) if lvl_m else -1
                    status_m = re.search(r"status:\s*(\d+)", batt_out)
                    charging = status_m.group(1) == "2" if status_m else False
                    batt_status = f"{level}%" + (" ~зарядка" if charging else "")

                    # Device Owner
                    owners = adb.shell("dpm list-owners", timeout=5)
                    has_owner = core.MDM_PACKAGE in owners
                    owner_text = "Headwind MDM" if has_owner else ("Сторонний" if owners.strip() else "НЕТ")

                    # Аккаунты
                    accs = adb.shell("dumpsys account", timeout=5)
                    google_acc = "—"
                    g_match = re.search(r"Account \{name=([^\s,]+), type=com\.google\}", accs)
                    if g_match:
                        google_acc = g_match.group(1)

                    telem = DeviceTelemetry(
                        serial=target,
                        brand=brand,
                        model=model,
                        android=release,
                        api=sdk,
                        battery_level=level,
                        battery_status=batt_status,
                        device_owner=owner_text,
                        google_account=google_acc,
                        online=True,
                        status_text="Готов к обработке (нажмите Пробел или авто)" if self.paused else "Готов к обработке",
                    )
                    self.event_queue.put(("telemetry", telem))
            except Exception as exc:                  # noqa: BLE001
                # молчать здесь нельзя: оператор увидит вечное «ожидание
                # планшета» и не узнает, что adb вообще не отвечает
                message = f"опрос устройств не удался: {exc}"
                if message != last_poll_error:
                    last_poll_error = message
                    self.event_queue.put(("log", "err", message))
            time.sleep(1.2)

    def _pipeline_worker(self) -> None:
        """Конвейерный обработчик устройств."""
        processed_serials = set()

        while not self.should_exit:
            if self.paused or not self.connected_serials:
                time.sleep(0.4)
                continue

            target_serial = self.connected_serials[0]
            if target_serial in processed_serials and self.options.conveyor:
                # Ждём смены кабеля / переподключения
                time.sleep(0.5)
                continue

            # Начинаем работу с устройством
            self.is_processing = True
            self.active_serial = target_serial
            self.event_queue.put(("log", "step", f"Начало обработки: {target_serial}"))

            # Сброс матрицы этапов
            for k, _, _ in STAGE_DEFINITIONS:
                self.event_queue.put(("stage_update", k, "queued" if self.options.stages.get(k, True) else "skip", ""))

            log_dir = os.path.join(os.path.dirname(__file__), "logs")
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = os.path.join(log_dir, f"tui3_{target_serial}_{stamp}.log")

            args = self.options.to_cleanup_args(target_serial, log_path)
            db_logger = DashboardLogger(self.event_queue, log_path)

            summary = core.Summary(serial=target_serial)
            stages_to_run = [k for k, _, _ in STAGE_DEFINITIONS if self.options.stages.get(k, True)]

            total_failed = False
            orig_confirm = core.confirm
            core.confirm = lambda q, default=False, auto=False: True
            try:
                for idx, stage_key in enumerate(stages_to_run):
                    if self.should_exit:
                        break
                    self.running_stage_idx = idx
                    self.event_queue.put(("stage_update", stage_key, "running", "выполняется..."))

                    stage_args = argparse.Namespace(**vars(args))
                    stage_args.only = stage_key
                    # Движок принимает один этап за проход, а пролог (проверка
                    # и обновление MDM, назначение владельца) в каждом проходе
                    # повторяется. Делаем его один раз, иначе на планшет
                    # уходит десяток лишних минут и десять sha256sum по 8 МБ.
                    if idx:
                        stage_args.install_mdm = False
                        stage_args.reinstall_mdm = "never"
                        stage_args.no_set_owner = True
                    # строку учёта пишем один раз, когда всё уже сделано
                    if idx != len(stages_to_run) - 1:
                        stage_args.inventory = ""

                    try:
                        res = core.process_device(target_serial, stage_args, db_logger)
                        if res:
                            summary = core.merge_summaries(summary, res)
                        self.event_queue.put(("stage_update", stage_key, "ok", "готово"))
                    except Exception as exc:
                        self.event_queue.put(("stage_update", stage_key, "err", str(exc)[:20]))
                        db_logger.err(f"Ошибка этапа {stage_key}: {exc}")
                        total_failed = True
            finally:
                core.confirm = orig_confirm
                db_logger.close()

            self.running_stage_idx = -1
            self.is_processing = False
            self.processed_count += 1
            if total_failed or summary.failed or summary.aborted:
                self.errors_count += 1

            self.event_queue.put(("summary", summary))
            self.event_queue.put(("log", "ok", f"Обработка {target_serial} завершена! Отключите кабель."))
            processed_serials.add(target_serial)

            # Ждём отключения устройства при конвейере
            while target_serial in self.connected_serials and not self.should_exit:
                time.sleep(0.5)

            if target_serial not in self.connected_serials:
                processed_serials.discard(target_serial)
                self.active_serial = None
                self.event_queue.put(("log", "info", f"Планшет {target_serial} отключён. Ожидание следующего..."))

            if not self.options.conveyor:
                self.paused = True

    # ──────────────────────── Отрисовка UI ────────────────────────

    def _render(self) -> None:
        self.screen.erase()
        h, w = self.screen.getmaxyx()

        if h < 24 or w < 80:
            safe_addstr(self.screen, h // 2, 2, "Требуется размер терминала не менее 80x24", curses.A_BOLD)
            safe_addstr(self.screen, h // 2 + 1, 2, f"Текущий размер: {w}x{h} (измените размер окна)", curses.color_pair(C_WARN))
            self.screen.refresh()
            return

        self._draw_header(w)
        self._draw_left_panel(h, w)
        self._draw_right_panel(h, w)
        self._draw_footer(h, w)

        # Отрисовка активных модальных окон
        if self.active_modal == "stages":
            self._draw_modal_stages(h, w)
        elif self.active_modal == "passport":
            self._draw_modal_passport(h, w)
        elif self.active_modal == "inventory":
            self._draw_modal_inventory(h, w)
        elif self.active_modal == "devices":
            self._draw_modal_devices(h, w)

        self.screen.refresh()

    def _draw_header(self, width: int) -> None:
        # Верхняя статусная плашка
        title = f" WUNDER TABLET · CONTROL DASHBOARD v{DASHBOARD_VERSION} "
        mode_badge = f" [ РЕЖИМ: {self.options.mode.upper()} ] "
        conveyor_badge = " [ КОНВЕЙЕР: ВКЛ ] " if self.options.conveyor else " [ РАЗОВЫЙ ] "
        stats = f" Успешно: {self.processed_count} | Ошибки: {self.errors_count} "
        now_time = datetime.datetime.now().strftime("%H:%M:%S")

        safe_addstr(self.screen, 0, 0, " " * (width - 1), curses.color_pair(C_SEL))
        safe_addstr(self.screen, 0, 1, title, curses.color_pair(C_SEL) | curses.A_BOLD)
        safe_addstr(self.screen, 0, 42, mode_badge, curses.color_pair(C_SEL))
        safe_addstr(self.screen, 0, 58, conveyor_badge, curses.color_pair(C_SEL))
        safe_addstr(self.screen, 0, max(60, width - len(stats) - 10), stats, curses.color_pair(C_SEL) | curses.A_BOLD)
        safe_addstr(self.screen, 0, width - 9, now_time, curses.color_pair(C_SEL))

    def _draw_left_panel(self, h: int, w: int) -> None:
        """Левая панель: телеметрия устройства + параметры."""
        left_w = 38
        top_y = 1
        panel_h = h - 3

        # Отрисовка рамки панели
        self._draw_box(top_y, 0, panel_h, left_w, " ПЛАНШЕТ И СТАТУС ", C_PANEL_BOX)

        t = self.telemetry
        line = top_y + 1
        # значения идут с 15-й колонки, справа рамка: всё, что длиннее,
        # раньше перечёркивало границу панели
        val_w = left_w - 16

        # Статус соединения
        if t.online:
            st_color = curses.color_pair(C_OK) | curses.A_BOLD
            st_icon = "● ПОДКЛЮЧЕН"
        else:
            st_color = curses.color_pair(C_WARN) | curses.A_BOLD
            st_icon = "○ НЕТ СВЯЗИ"

        safe_addstr(self.screen, line, 2, "Статус ADB:", curses.color_pair(C_DIM))
        safe_addstr(self.screen, line, 15, st_icon, st_color)
        line += 1

        safe_addstr(self.screen, line, 2, "Серийный:", curses.color_pair(C_DIM))
        safe_addstr(self.screen, line, 15, (t.serial or "—")[:val_w], curses.A_BOLD)
        line += 1

        safe_addstr(self.screen, line, 2, "Устройство:", curses.color_pair(C_DIM))
        safe_addstr(self.screen, line, 15, f"{t.brand} {t.model}"[:val_w], curses.A_BOLD)
        line += 1

        safe_addstr(self.screen, line, 2, "Android:", curses.color_pair(C_DIM))
        safe_addstr(self.screen, line, 15, f"{t.android} (API {t.api})"[:val_w], curses.color_pair(C_INFO))
        line += 1

        # Батарея со шкалой
        safe_addstr(self.screen, line, 2, "Батарея:", curses.color_pair(C_DIM))
        if t.battery_level >= 0:
            gauge_w = 8
            filled = int((t.battery_level / 100.0) * gauge_w)
            bar = "█" * filled + "░" * (gauge_w - filled)
            color = C_GAUGE_HIGH if t.battery_level > 50 else (C_GAUGE_MID if t.battery_level > 20 else C_GAUGE_LOW)
            safe_addstr(self.screen, line, 15, f"[{bar}] {t.battery_status}"[:val_w], curses.color_pair(color))
        else:
            safe_addstr(self.screen, line, 15, "—", curses.color_pair(C_DIM))
        line += 1

        # Device Owner
        safe_addstr(self.screen, line, 2, "Владелец:", curses.color_pair(C_DIM))
        ow_color = C_OK if "Headwind" in t.device_owner else (C_ERR if t.device_owner == "НЕТ" else C_WARN)
        safe_addstr(self.screen, line, 15, t.device_owner[:val_w], curses.color_pair(ow_color) | curses.A_BOLD)
        line += 1

        # Google Account
        safe_addstr(self.screen, line, 2, "Google:", curses.color_pair(C_DIM))
        g_color = C_OK if t.google_account != "—" else C_WARN
        safe_addstr(self.screen, line, 15, t.google_account[:val_w], curses.color_pair(g_color))
        line += 2

        # Разделитель
        safe_addstr(self.screen, line, 0, "├" + "─" * (left_w - 2) + "┤", curses.color_pair(C_PANEL_BOX))
        safe_addstr(self.screen, line, 2, " ТЕКУЩИЙ КОНВЕЙЕР ", curses.color_pair(C_HEADER))
        line += 1

        # Состояние выполнения
        state_title = "В РАБОТЕ" if self.is_processing else ("ПАУЗА" if self.paused else "ОЖИДАНИЕ")
        state_color = C_STAGE_ACTIVE if self.is_processing else (C_WARN if self.paused else C_OK)
        safe_addstr(self.screen, line, 2, "Состояние:", curses.color_pair(C_DIM))
        safe_addstr(self.screen, line, 15, state_title[:val_w], curses.color_pair(state_color) | curses.A_BOLD)
        line += 1

        safe_addstr(self.screen, line, 2, "Инфо:", curses.color_pair(C_DIM))
        safe_addstr(self.screen, line, 8, t.status_text[:left_w - 9], curses.color_pair(C_INFO))
        line += 2

        # Разделитель
        safe_addstr(self.screen, line, 0, "├" + "─" * (left_w - 2) + "┤", curses.color_pair(C_PANEL_BOX))
        safe_addstr(self.screen, line, 2, " БЫСТРЫЕ ОПЦИИ ", curses.color_pair(C_HEADER))
        line += 1

        safe_addstr(self.screen, line, 2, f"• [S] Этапов активно: {sum(1 for v in self.options.stages.values() if v)}/{len(STAGE_DEFINITIONS)}")
        line += 1
        safe_addstr(self.screen, line, 2, f"• [C] Конвейер: {'ВКЛ' if self.options.conveyor else 'ВЫКЛ'}")
        line += 1
        safe_addstr(self.screen, line, 2, f"• Учёт в inventory: {'ДА' if self.options.inventory else 'НЕТ'}")
        line += 1
        safe_addstr(self.screen, line, 2, f"• Play Store: {'ОСТАВИТЬ' if self.options.keep_play else 'ВЫКЛЮЧИТЬ'}")

    def _draw_right_panel(self, h: int, w: int) -> None:
        """Правая панель: верхняя часть — матрица этапов, нижняя — живой лог."""
        left_w = 38
        right_x = left_w + 1
        right_w = w - right_x - 1
        top_y = 1

        stages_h = 13
        log_h = h - stages_h - 3

        # Верхняя панель: Матрица этапов
        self._draw_box(top_y, right_x, stages_h, right_w, " МАТРИЦА ЭТАПОВ И ПРОГРЕСС ", C_PANEL_BOX)

        # Прогресс-бар
        active_stages = [k for k, _, _ in STAGE_DEFINITIONS if self.options.stages.get(k, True)]
        total_st = len(active_stages)
        done_st = sum(1 for k in active_stages if self.stage_states[k] in ("ok", "warn", "err"))
        pct = int((done_st / total_st) * 100) if total_st > 0 else 0

        # «Прогресс: [» + шкала + «] 100%» = bar_len + 17 символов, плюс по
        # два на отступ и рамку с каждой стороны — иначе шкала съедает границу
        bar_len = max(10, right_w - 21)
        filled = int((pct / 100.0) * bar_len)
        bar_str = "█" * filled + "░" * (bar_len - filled)
        safe_addstr(self.screen, top_y + 1, right_x + 2, f"Прогресс: [{bar_str}] {pct:3d}%", curses.color_pair(C_ACCENT) | curses.A_BOLD)

        # Вывод этапов в 2 колонки
        col_w = (right_w - 4) // 2
        for idx, (key, title, desc) in enumerate(STAGE_DEFINITIONS):
            col = idx // 5
            row_idx = idx % 5
            y = top_y + 3 + (row_idx * 1)
            x = right_x + 2 + (col * col_w)

            st = self.stage_states.get(key, "idle")
            if st == "running":
                badge = "[ * В РАБОТЕ ]"
                attr = curses.color_pair(C_STAGE_ACTIVE) | curses.A_BOLD
            elif st == "ok":
                badge = "[ OK УСПЕШНО ]"
                attr = curses.color_pair(C_OK) | curses.A_BOLD
            elif st == "err":
                badge = "[ ! СБОЙ     ]"
                attr = curses.color_pair(C_ERR) | curses.A_BOLD
            elif st == "warn":
                badge = "[ ? ВНИМАНИЕ ]"
                attr = curses.color_pair(C_WARN) | curses.A_BOLD
            elif st == "skip":
                badge = "[ >> ПРОПУСК ]"
                attr = curses.color_pair(C_DIM)
            else:
                badge = "[ · В ОЧЕРЕДИ]"
                attr = curses.color_pair(C_DIM)

            safe_addstr(self.screen, y, x, badge, attr)
            safe_addstr(self.screen, y, x + 16, f"{title:<12}", curses.A_BOLD)

        # Нижняя панель: Живой терминал логов
        log_y = top_y + stages_h
        self._draw_box(log_y, right_x, log_h, right_w, " ЖИВОЙ ТЕРМИНАЛ СОБЫТИЙ ", C_PANEL_BOX)

        max_log_lines = log_h - 2
        visible_logs = list(self.log_lines)[-max_log_lines:]
        for idx, (tm, level, text) in enumerate(visible_logs):
            line_y = log_y + 1 + idx
            level_tag = f"[{level.upper():^4}]"

            if level in ("err", "error"):
                tag_attr = curses.color_pair(C_ERR) | curses.A_BOLD
            elif level in ("warn", "warning"):
                tag_attr = curses.color_pair(C_WARN) | curses.A_BOLD
            elif level in ("ok", "success"):
                tag_attr = curses.color_pair(C_OK) | curses.A_BOLD
            elif level == "step":
                tag_attr = curses.color_pair(C_ACCENT) | curses.A_BOLD
            else:
                tag_attr = curses.color_pair(C_INFO)

            safe_addstr(self.screen, line_y, right_x + 2, tm, curses.color_pair(C_DIM))
            safe_addstr(self.screen, line_y, right_x + 11, level_tag, tag_attr)
            safe_addstr(self.screen, line_y, right_x + 18, text[:right_w - 20])

    def _draw_footer(self, h: int, w: int) -> None:
        foot_y = h - 1
        safe_addstr(self.screen, foot_y, 0, " " * (w - 1), curses.color_pair(C_SEL))
        keys = (
            "[Пробел] Пауза/Старт  "
            "[S] Настройка этапов  "
            "[P] Паспорт  "
            "[I] Учёт (CSV)  "
            "[D] Устройства  "
            "[C] Конвейер  "
            "[Q] Выход"
        )
        safe_addstr(self.screen, foot_y, 1, keys, curses.color_pair(C_SEL) | curses.A_BOLD)

    def _draw_box(self, y: int, x: int, h: int, w: int, title: str, color_pair: int) -> None:
        """Отрисовывает красивую рамку Unicode с заголовком."""
        attr = curses.color_pair(color_pair)
        # Верх и низ
        safe_addstr(self.screen, y, x, "┌" + "─" * (w - 2) + "┐", attr)
        safe_addstr(self.screen, y + h - 1, x, "└" + "─" * (w - 2) + "┘", attr)
        # Боковины
        for dy in range(1, h - 1):
            safe_addstr(self.screen, y + dy, x, "│", attr)
            safe_addstr(self.screen, y + dy, x + w - 1, "│", attr)
        if title:
            safe_addstr(self.screen, y, x + 2, f"┤{title}├", attr | curses.A_BOLD)

    # ──────────────────────── Модальные окна ────────────────────────

    def _draw_modal_stages(self, h: int, w: int) -> None:
        # 3 строки сверху (рамка + подсказка + отступ), 2 снизу (footer + рамка)
        items_count = len(STAGE_DEFINITIONS) + 6
        mw, mh = min(70, w - 6), min(items_count + 5, h - 4)
        mx, my = (w - mw) // 2, (h - mh) // 2

        # Фон модалки
        for dy in range(mh):
            safe_addstr(self.screen, my + dy, mx, " " * mw, curses.color_pair(C_SEL))

        self._draw_box(my, mx, mh, mw, " НАСТРОЙКА ЭТАПОВ И ПАРАМЕТРОВ [S] ", C_SEL)

        safe_addstr(self.screen, my + 1, mx + 2, "Используйте ↑↓ для выбора, [Пробел]/[Enter] для переключения", curses.A_BOLD)

        items = []
        for key, title, desc in STAGE_DEFINITIONS:
            is_on = self.options.stages.get(key, True)
            check = "[X]" if is_on else "[ ]"
            items.append((f"{check} {title:<14} — {desc}", key, "stage"))

        items.append(("—" * (mw - 6), "", "sep"))
        items.append((f"Режим удаления: [{self.options.mode.upper()}] (auto / disable / uninstall)", "mode", "opt"))
        items.append((f"Google Play: [{'ОСТАВИТЬ' if self.options.keep_play else 'ВЫКЛЮЧИТЬ'}]", "play", "opt"))
        items.append((f"Блокировка настроек: [{'ВКЛ' if self.options.lock_settings else 'ВЫКЛ'}]", "lock", "opt"))
        items.append((f"Снос сторов в thirdparty: [{'ВКЛ' if self.options.block_stores else 'ВЫКЛ'}]", "block_stores", "opt"))
        items.append((f"Обновлять MDM из hmdm.apk: "
                      f"[{ {'auto': 'ЕСЛИ СБОРКА ДРУГАЯ', 'force': 'ВСЕГДА', 'never': 'НЕ ТРОГАТЬ'}[self.options.reinstall_mdm] }]",
                      "reinstall_mdm", "opt"))

        for idx, (label, key, itype) in enumerate(items[:mh - 5]):
            y = my + 3 + idx
            is_sel = idx == self.modal_selection
            attr = curses.color_pair(C_STAGE_ACTIVE) | curses.A_BOLD if is_sel else 0
            safe_addstr(self.screen, y, mx + 3, label[:mw - 6], attr)

        safe_addstr(self.screen, my + mh - 2, mx + 2, "[Esc] / [S] Закрыть и применить", curses.A_BOLD)

    def _draw_modal_passport(self, h: int, w: int) -> None:
        mw, mh = min(74, w - 6), min(22, h - 4)
        mx, my = (w - mw) // 2, (h - mh) // 2

        for dy in range(mh):
            safe_addstr(self.screen, my + dy, mx, " " * mw, curses.color_pair(C_DEFAULT))

        self._draw_box(my, mx, mh, mw, " ПАСПОРТ УСТРОЙСТВА [P] ", C_HEADER)

        s = self.last_summary
        if not s:
            safe_addstr(self.screen, my + mh // 2, mx + 4, "Пока нет данных о завершённых прогонах на этой сессии.", curses.color_pair(C_WARN))
        else:
            lines = [
                f"Устройство:        {s.brand} {s.model} ({s.serial})",
                f"Android:            {s.android} · Build: {s.build}",
                f"Владелец (Owner):   {s.owner_label or s.owner_component or 'НЕ НАЗНАЧЕН'}",
                f"Домашний экран:     {s.home_now or '—'}",
                f"Браузер по умолч.:  {s.browser_now or '—'}",
                f"Язык / Время:       {s.locale_now or '—'} / {'Автовремя ВКЛ' if s.time_auto else '—'}",
                f"Лаунчеры удалено:   {len(s.launchers_removed)} | отключено: {len(s.launchers_disabled)}",
                f"Браузеры удалено:   {len(s.browsers_removed)} | отключено: {len(s.browsers_disabled)}",
                f"Ассистенты:         {len(s.assistants_removed)} | отключено: {len(s.assistants_disabled)}",
                f"Сторонние (игры):   {len(s.thirdparty_removed)} | заглушено: {s.muted}",
                f"Магазины сняты:     {len(s.stores_disabled) + len(s.stores_removed)}",
                f"Таблица учёта:      {s.inventory_status or 'не записан'}",
            ]
            for idx, ln in enumerate(lines[:mh - 4]):
                safe_addstr(self.screen, my + 2 + idx, mx + 3, ln[:mw - 6])

        safe_addstr(self.screen, my + mh - 2, mx + 2, "[Esc] / [P] Закрыть", curses.A_BOLD)

    def _draw_modal_inventory(self, h: int, w: int) -> None:
        mw, mh = min(78, w - 4), min(22, h - 4)
        mx, my = (w - mw) // 2, (h - mh) // 2

        for dy in range(mh):
            safe_addstr(self.screen, my + dy, mx, " " * mw, curses.color_pair(C_DEFAULT))

        self._draw_box(my, mx, mh, mw, " ТАБЛИЦА УЧЁТА (inventory.csv) [I] ", C_HEADER)

        inv_path = os.path.join(os.path.dirname(__file__), "inventory.csv")
        rows = core.inventory_read(inv_path)

        if not rows:
            safe_addstr(self.screen, my + mh // 2, mx + 4, "Файл inventory.csv пуст или не найден.", curses.color_pair(C_WARN))
        else:
            hdr = f"{'ДАТА':<12} {'СЕРИЙНЫЙ':<14} {'МОДЕЛЬ':<12} {'УЧЕНИК':<16} {'КЛАСС':<6}"
            safe_addstr(self.screen, my + 2, mx + 3, hdr[:mw - 6], curses.color_pair(C_HEADER) | curses.A_BOLD)
            safe_addstr(self.screen, my + 3, mx + 3, "─" * (mw - 6), curses.color_pair(C_DIM))

            last_rows = rows[-12:]
            for idx, r in enumerate(reversed(last_rows)):
                y = my + 4 + idx
                if y >= my + mh - 2:
                    break
                row_str = f"{r.get('дата', '')[:10]:<12} {r.get('серийный', '')[:13]:<14} {r.get('модель', '')[:11]:<12} {r.get('ФИО ученика', '')[:15]:<16} {r.get('класс', '')[:5]:<6}"
                safe_addstr(self.screen, y, mx + 3, row_str[:mw - 6])

        safe_addstr(self.screen, my + mh - 2, mx + 2, f"Всего записей: {len(rows)} | [Esc] / [I] Закрыть", curses.A_BOLD)

    def _draw_modal_devices(self, h: int, w: int) -> None:
        mw, mh = min(60, w - 6), min(16, h - 4)
        mx, my = (w - mw) // 2, (h - mh) // 2

        for dy in range(mh):
            safe_addstr(self.screen, my + dy, mx, " " * mw, curses.color_pair(C_DEFAULT))

        self._draw_box(my, mx, mh, mw, " ПОДКЛЮЧЁННЫЕ УСТРОЙСТВА [D] ", C_HEADER)

        if not self.connected_serials:
            safe_addstr(self.screen, my + mh // 2, mx + 4, "Нет активных ADB устройств.", curses.color_pair(C_WARN))
        else:
            safe_addstr(self.screen, my + 2, mx + 3, "Список обнаруженных устройств (ADB):", curses.A_BOLD)
            for idx, s in enumerate(self.connected_serials[:mh - 6]):
                y = my + 4 + idx
                is_cur = s == self.active_serial
                badge = "[АКТИВНОЕ]" if is_cur else "[ГОТОВО]  "
                attr = curses.color_pair(C_OK) if is_cur else curses.color_pair(C_INFO)
                safe_addstr(self.screen, y, mx + 3, f"{badge}  {s}", attr | curses.A_BOLD)

        safe_addstr(self.screen, my + mh - 2, mx + 2, "[Esc] / [D] Закрыть", curses.A_BOLD)

    # ──────────────────────── Ввод и события ────────────────────────

    def _handle_input(self) -> None:
        try:
            ch = self.screen.getch()
        except curses.error:
            return

        if ch == -1:
            return

        # Модальное окно активно
        if self.active_modal is not None:
            if ch in (27, ord('q'), ord('Q')):  # Esc
                if self.active_modal == "stages":
                    save_dashboard_config(self.options)
                self.active_modal = None
                return
            if self.active_modal == "stages":
                self._handle_stages_modal_input(ch)
            elif self.active_modal in ("passport", "inventory", "devices"):
                if ch in (ord('p'), ord('P'), ord('i'), ord('I'), ord('d'), ord('D')):
                    self.active_modal = None
            return

        # Главный экран
        if ch in (ord('q'), ord('Q')):
            save_dashboard_config(self.options)
            self.should_exit = True
        elif ch == ord(' '):  # Пробел: пауза / запуск
            self.paused = not self.paused
            self.event_queue.put(("log", "info", "Конвейер ПРИОСТАНОВЛЕН" if self.paused else "Конвейер ВОЗОБНОВЛЁН"))
        elif ch in (ord('s'), ord('S')):
            self.active_modal = "stages"
            self.modal_selection = 0
        elif ch in (ord('p'), ord('P')):
            self.active_modal = "passport"
        elif ch in (ord('i'), ord('I')):
            self.active_modal = "inventory"
        elif ch in (ord('d'), ord('D')):
            self.active_modal = "devices"
        elif ch in (ord('c'), ord('C')):
            self.options.conveyor = not self.options.conveyor
            save_dashboard_config(self.options)
            self.event_queue.put(("log", "info", f"Режим конвейера: {'ВКЛ' if self.options.conveyor else 'ВЫКЛ'}"))

    def _handle_stages_modal_input(self, ch: int) -> None:
        # этапы + разделитель + пять параметров (режим, Play, замок, сторы, MDM)
        total_items = len(STAGE_DEFINITIONS) + 6
        if ch == curses.KEY_UP:
            self.modal_selection = (self.modal_selection - 1) % total_items
            if self.modal_selection == len(STAGE_DEFINITIONS):  # Пропуск разделителя
                self.modal_selection -= 1
        elif ch == curses.KEY_DOWN:
            self.modal_selection = (self.modal_selection + 1) % total_items
            if self.modal_selection == len(STAGE_DEFINITIONS):
                self.modal_selection += 1
        elif ch in (10, 13, ord(' ')):
            sel = self.modal_selection
            if sel < len(STAGE_DEFINITIONS):
                key = STAGE_DEFINITIONS[sel][0]
                self.options.stages[key] = not self.options.stages.get(key, True)
            elif sel == len(STAGE_DEFINITIONS) + 1:  # mode
                modes = ["auto", "disable", "uninstall"]
                cur_i = modes.index(self.options.mode) if self.options.mode in modes else 0
                self.options.mode = modes[(cur_i + 1) % len(modes)]
            elif sel == len(STAGE_DEFINITIONS) + 2:  # keep play
                self.options.keep_play = not self.options.keep_play
            elif sel == len(STAGE_DEFINITIONS) + 3:  # lock settings
                self.options.lock_settings = not self.options.lock_settings
            elif sel == len(STAGE_DEFINITIONS) + 4:  # block stores
                self.options.block_stores = not self.options.block_stores
            elif sel == len(STAGE_DEFINITIONS) + 5:  # обновление MDM
                order = ["auto", "force", "never"]
                current = order.index(self.options.reinstall_mdm)
                self.options.reinstall_mdm = order[(current + 1) % len(order)]
            save_dashboard_config(self.options)


def main() -> None:
    curses.wrapper(lambda stdscr: DashboardApp(stdscr).start())


if __name__ == "__main__":
    main()
